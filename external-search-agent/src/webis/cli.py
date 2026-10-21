#!/usr/bin/env python3
"""WEBIS-SEARCH CLI.

  python -m webis.cli search --subject "Meridian Trade Holdings Ltd"
  python -m webis.cli search --subject "Meridian Trade Holdings Ltd" --budget-fetches 6
  python -m webis.cli verify                       re-verify every quote against its content hash
  python -m webis.cli ledger                       verify the evidence chain
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from webis.agent import SearchAgent
from webis.evidence import EvidenceLedger, IdempotencyStore
from webis.fetch import Fetcher, SourcePolicy, sha256

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "out"


def _setup(budget_fetches: int | None = None, resume: bool = True):
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    budget = yaml.safe_load((ROOT / "config" / "budget.yaml").read_text())["budget"]
    if budget_fetches:
        budget["max_fetches"] = budget_fetches
    policy = SourcePolicy(cfg)
    cache = OUT / "cache" if resume else None
    fetcher = Fetcher(policy, cache_dir=cache)
    ledger = EvidenceLedger(OUT / "search_evidence.jsonl", agent_id="WEBIS-SEARCH",
                            idem=IdempotencyStore(OUT / "state" / "idem.json"))
    return policy, fetcher, ledger, budget, cfg


def cmd_search(args) -> int:
    policy, fetcher, ledger, budget, cfg = _setup(args.budget_fetches)
    request = {
        "request_id": args.request_id,
        "subject": args.subject,
        "facets": (args.facets.split(",") if args.facets else None),
        "max_queries": args.max_queries,
        "related_parties": (args.related_party or []),
        "attributes": ({"dob": args.dob, "country": args.country} if (args.dob or args.country) else {}),
        "retrieved_at": args.retrieved_at,
    }
    state_path = OUT / "state" / f"run_{args.request_id}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"another run holds {lock}; refusing to race the same request id", file=sys.stderr)
        return 4
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    checkpoint = None
    if args.resume and state_path.exists():
        checkpoint = json.loads(state_path.read_text())
        ledger.state(event="RESUMED", detail={"request_id": args.request_id,
                                             "from_query_index": checkpoint.get("query_index")})
        print(f"[resume] {args.request_id}: {checkpoint.get('query_index')} query/queries and "
              f"{len(checkpoint.get('fetches') or {})} fetch(es) restored from {state_path}")
    else:
        state_path.unlink(missing_ok=True)

    def save(state: dict) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))          # atomic: a kill mid-write is harmless
        tmp.replace(state_path)

    agent = SearchAgent(fetcher, policy, budget=budget, on_checkpoint=save,
                        stop_after_queries=args.stop_after)
    res = agent.run(request, checkpoint=checkpoint)

    if res.status == "PARTIAL" and args.stop_after is not None:
        (OUT / "partial_result.json").write_text(
            json.dumps({"request_id": res.request_id, "queries_done": res.cost.get("queries"),
                        "fetches": res.cost.get("fetches"), "gaps": res.gaps}, indent=2))
        print(f"[interrupted] {res.cost.get('queries')} query/queries done, "
              f"{res.cost.get('fetches')} fetch(es); resume with --resume")
        _unlock(lock)
        return 3
    pack = agent.pack(res)
    pack["pack_id"] = "PACK-" + sha256(json.dumps(pack, sort_keys=True))[7:19]
    pack["source_policy_version"] = cfg["version"]

    for q in res.queries:
        qd = q if isinstance(q, dict) else q.__dict__
        ledger.query(request_id=pack["request_id"],
                     query={"query_id": qd["query_id"], "text": qd["text"],
                            "facet": qd.get("facet"), "rationale": qd.get("rationale")})
    for claim in pack["claims"]:
        ledger.claim(claim=claim, request_id=pack["request_id"])
    for q in pack["quarantined_sources"]:
        ledger.quarantine(source=q)
    for r in pack["refused_sources"]:
        ledger.refusal(source=r)
    ledger.pack(pack=pack)

    (OUT / "evidence_pack.json").write_text(json.dumps(pack, indent=2) + "\n")
    # a run that stopped on a budget keeps its checkpoint, so raising the budget resumes it rather
    # than starting over; a run that finished its plan clears it
    budget_gaps = {"FETCH_BUDGET_EXHAUSTED", "WALLCLOCK_BUDGET_EXHAUSTED", "DOMAIN_BUDGET_EXHAUSTED"}
    if not ({g["gap"] for g in pack["gaps"]} & budget_gaps):
        state_path.unlink(missing_ok=True)
    _unlock(lock)
    _print_pack(pack)
    return 0


def _print_pack(pack: dict) -> None:
    print(f"\n=== {pack['request_id']} · {pack['subject']} · {pack['status']} ===")
    for c in pack["claims"]:
        print(f"\n  [{c['confidence']:<7}] {c['claim']}")
        print(f"            corroboration: {c['corroboration_count']} independent origin(s) "
              f"across {len(c['independent_domains'])} domain(s)")
        for cit in c["citations"][:3]:
            print(f"            - {cit['url']}  (tier {cit['source_tier']}, "
                  f"credibility {cit['credibility']['score']}) {cit['evidence_id']}")
            extra = f"  (+{len(cit['evidence_ids']) - 1} more passage(s) from this document)" \
                if len(cit.get("evidence_ids", [])) > 1 else ""
            print(f"              \"{cit['quote'][:100]}…\"{extra}")
        if c.get("refresh_due"):
            age = c.get("newest_source_age_days")
            print(f"            ~ refresh due: newest source is "
                  f"{'undated' if age is None else str(age) + ' days old'}, "
                  f"limit {c.get('refresh_limit_days')} day(s) for this claim family")
        for note in c["notes"][:2]:
            print(f"            ! {note}")
        for con in c["contradictions"][:2]:
            print(f"            x contradicted by {con['url']}: \"{con['quote'][:80]}…\"")
    if pack["quarantined_sources"]:
        print("\n  QUARANTINED")
        for q in pack["quarantined_sources"]:
            print(f"    - {q['url']}: {', '.join(q['patterns'])}")
    if pack["refused_sources"]:
        print("\n  REFUSED")
        for r in pack["refused_sources"]:
            print(f"    - {r['url']}: {r['status']} ({r['reason']})")
    if pack.get("unverified_circulation"):
        print("\n  CIRCULATING — recorded, not relied upon")
        for u in pack["unverified_circulation"]:
            print(f"    - {u['url']}")
            print(f"      {u['note']}")
    if pack["gaps"]:
        print("\n  GAPS")
        for g in pack["gaps"]:
            print(f"    - {g['gap']}: {g['detail'][:100]}")
    print(f"\n  cost: {pack['cost']}")


def _unlock(path: pathlib.Path) -> None:
    path.unlink(missing_ok=True)


def cmd_verify(args) -> int:
    """Re-verify every stored quote: does the exact text still appear at the recorded span in a
    document whose content hash matches? This is the citation-integrity check."""
    path = OUT / "evidence_pack.json"
    if not path.exists():
        print("no evidence pack — run `search` first")
        return 1
    pack = json.loads(path.read_text())
    checked = failed = 0
    problems = []
    corpus = {}
    for f in (ROOT / "corpus" / "sites").rglob("*.json"):
        doc = json.loads(f.read_text())
        corpus[doc["url"]] = doc
    for claim in pack["claims"]:
        for cit in claim["citations"]:
            checked += 1
            doc = corpus.get(cit["url"])
            if not doc:
                problems.append(f"{cit['evidence_id']}: source not in corpus")
                failed += 1
                continue
            span_text = doc["body"][cit["char_span"][0]:cit["char_span"][1]]
            if sha256(doc["body"]) != cit["content_hash"]:
                problems.append(f"{cit['evidence_id']}: content hash mismatch")
                failed += 1
            elif cit.get("span_text_sha256") and sha256(span_text) != cit["span_text_sha256"]:
                problems.append(f"{cit['evidence_id']}: span text does not hash to the recorded value")
                failed += 1
            elif not cit.get("pii_redactions") and span_text != cit["quote"]:
                problems.append(f"{cit['evidence_id']}: quote does not match the recorded span")
                failed += 1
    print(json.dumps({"quotes_checked": checked, "failed": failed,
                      "quote_precision": round((checked - failed) / checked * 100, 2) if checked else None,
                      "problems": problems[:10]}, indent=2))
    return 0 if failed == 0 else 2


def cmd_ledger(args) -> int:
    _, _, ledger, _, _ = _setup()
    ok, head, broken = ledger.verify_chain()
    recs = list(ledger.records())
    types: dict[str, int] = {}
    for r in recs:
        types[r["record_type"]] = types.get(r["record_type"], 0) + 1
    print(json.dumps({"chain_valid": ok, "head": head, "broken_at": broken,
                      "records": len(recs), "by_type": types}, indent=2))
    return 0 if ok else 2


def main() -> int:
    p = argparse.ArgumentParser(prog="webis", description="external search agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--subject", required=True)
    s.add_argument("--request-id", default="SR-0001")
    s.add_argument("--facets", default=None)
    s.add_argument("--max-queries", type=int, default=12)
    s.add_argument("--budget-fetches", type=int, default=None)
    s.add_argument("--retrieved-at", default="2026-10-21T12:00:00Z")
    s.add_argument("--related-party", action="append", default=None,
                   help="screen a principal in their own right (repeatable)")
    s.add_argument("--resume", action="store_true",
                   help="continue a previous run of this request id from its checkpoint")
    s.add_argument("--stop-after", type=int, default=None,
                   help="stop after N queries and checkpoint (simulates a crash or a time limit)")
    s.add_argument("--dob", default=None, help="subject date of birth, used for same-name exclusion")
    s.add_argument("--country", default=None, help="subject country, used for same-name exclusion")
    s.set_defaults(func=cmd_search)

    v = sub.add_parser("verify")
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("ledger")
    l.set_defaults(func=cmd_ledger)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
