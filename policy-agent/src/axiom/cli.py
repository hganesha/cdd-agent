#!/usr/bin/env python3
"""AXIOM-POLICY CLI.

  python -m axiom.cli decide --input case.json      evaluate one policy decision (+ evidence)
  python -m axiom.cli authority --action case.close  authorize an agent action
  python -m axiom.cli check                          compile policy under restricted capabilities
  python -m axiom.cli test                           run the Rego test suite
  python -m axiom.cli build --revision r-001         build + verify + stage a bundle
  python -m axiom.cli verify --revision r-001        re-verify a built bundle against sources
  python -m axiom.cli author --doc corpus/...md --proposal P-001 --change change.json
  python -m axiom.cli corpus                          run the decision corpus against the active policy
  python -m axiom.cli ledger                          verify the decision evidence chain
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom.author import PolicyAuthor
from axiom.bundle import PolicyBundleService
from axiom.evidence import DecisionLedger, IdempotencyStore
from axiom.opa import OPAClient, OPAError

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"


def _ctx():
    opa = OPAClient()
    ledger = DecisionLedger(OUT / "decision_evidence.jsonl", agent_id="AXIOM-POLICY",
                            idem=IdempotencyStore(OUT / "state" / "idem.json"))
    return opa, ledger


def cmd_decide(args) -> int:
    opa, ledger = _ctx()
    payload = json.loads(Path(args.input).read_text())
    svc = PolicyBundleService(opa)
    bundle = svc.active() or svc.build("dev-unbuilt")
    t0 = time.perf_counter()
    result = opa.eval("data.agent.decision.result", data_paths=[ROOT / "policy"], input_obj=payload)
    elapsed = (time.perf_counter() - t0) * 1000
    decision = result.value
    if decision is None:
        print(json.dumps({"error": "policy returned no decision (undefined)"}, indent=2))
        return 2
    ev = ledger.decision(decision=decision, input_obj=payload, bundle=bundle, elapsed_ms=elapsed)
    decision["evidence_id"] = ev
    if args.explain:
        decision["_explain"] = opa.explain("data.agent.decision.result",
                                           data_paths=[ROOT / "policy"], input_obj=payload)[:4000]
    (OUT / "last_decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    summary = {k: decision[k] for k in ("decision_id", "outcome", "reasons", "obligations",
                                        "evidence_id")}
    summary["risk"] = {k: decision["risk"][k] for k in ("score", "band", "weighted_score_band",
                                                        "cdd_level")}
    summary["gates"] = decision["gates"]["required_gates"]
    summary["ubo"] = {"beneficial_owners": decision["ubo"]["beneficial_owners"],
                      "unresolved_pct": decision["ubo"]["unresolved_pct"]}
    summary["coverage"] = decision["coverage"]["coverage_pct"]
    print(json.dumps(summary, indent=2))
    return 0


def cmd_authority(args) -> int:
    opa, ledger = _ctx()
    r = opa.eval("data.agent.authority.result", data_paths=[ROOT / "policy"],
                 input_obj={"action": args.action})
    if r.value is None:
        print(json.dumps({"error": "authority returned no decision"}, indent=2))
        return 2
    ev = ledger.authority(action=args.action, result=r.value, actor=args.actor)
    print(json.dumps({**r.value, "evidence_id": ev}, indent=2))
    return 0 if r.value["decision"] == "allowed" else 3


def cmd_check(args) -> int:
    opa, ledger = _ctx()
    ok, msg = opa.check(ROOT / "policy", restricted=True, strict=args.strict)
    print(f"compile: {'PASS' if ok else 'FAIL'} (restricted capabilities, "
          f"denied: {', '.join(__import__('axiom.opa', fromlist=['DENIED_BUILTINS']).DENIED_BUILTINS)})")
    if msg:
        print(msg[:2000])
    unformatted = opa.fmt_check(ROOT / "policy")
    print(f"fmt: {'PASS' if not unformatted else 'FAIL ' + str(unformatted)}")
    ledger.state(event="policy_check", detail={"ok": ok, "strict": args.strict})
    return 0 if ok else 1


def cmd_test(args) -> int:
    opa, ledger = _ctx()
    ok, out = opa.test(ROOT / "policy", verbose=args.verbose)
    print(out[-4000:])
    passed = out.count(": PASS")
    failed = out.count(": FAIL")
    ledger.test_run(scope="opa test policy/", passed=passed, failed=failed, detail=out[-1500:])
    return 0 if ok else 1


def cmd_build(args) -> int:
    opa, ledger = _ctx()
    svc = PolicyBundleService(opa)
    manifest = svc.build(args.revision)
    ledger.bundle_event(event="BUILT", manifest=manifest)
    verification = svc.verify(args.revision)
    ledger.bundle_event(event="VERIFIED", manifest=verification["manifest"])
    print(json.dumps({"revision": manifest["revision"],
                      "policy_version": manifest["policy_version"],
                      "sha256": manifest["sha256"],
                      "files": manifest["source_file_count"],
                      "tests": manifest["tests"],
                      "restricted_capabilities": manifest["restricted_capabilities"],
                      "verified": verification["valid"],
                      "problems": verification["problems"],
                      "status": manifest["status"]}, indent=2))
    return 0 if verification["valid"] else 1


def cmd_verify(args) -> int:
    opa, ledger = _ctx()
    svc = PolicyBundleService(opa)
    v = svc.verify(args.revision)
    ledger.bundle_event(event="VERIFIED" if v["valid"] else "VERIFY_FAILED", manifest=v["manifest"])
    print(json.dumps({"revision": v["revision"], "valid": v["valid"], "problems": v["problems"]},
                     indent=2))
    return 0 if v["valid"] else 1


def cmd_author(args) -> int:
    opa, ledger = _ctx()
    author = PolicyAuthor(opa)
    change = json.loads(Path(args.change).read_text())
    proposal = author.propose(Path(args.doc), proposal_id=args.proposal, change=change)
    ledger.proposal(proposal=proposal)
    print(json.dumps({k: proposal[k] for k in ("proposal_id", "status", "source", "validation",
                                               "impact", "required_approvals", "activation")},
                     indent=2))
    print(f"\nproposal written to {proposal['_path']}")
    return 0 if proposal["validation"]["check_ok"] and proposal["validation"]["tests_ok"] else 1


def cmd_corpus(args) -> int:
    """Run every corpus case against its PINNED expectation. A mismatch is an undeclared policy
    change: it exits non-zero and is recorded as a failed run, not printed and forgotten."""
    opa, ledger = _ctx()
    expected_path = ROOT / "corpus" / "expected.json"
    expected = (json.loads(expected_path.read_text())["cases"] if expected_path.exists() else {})
    cases = sorted((ROOT / "corpus" / "decisions").glob("*.json"))
    rows, failures = [], []
    for f in cases:
        payload = json.loads(f.read_text())
        r = opa.eval("data.agent.decision.result", data_paths=[ROOT / "policy"], input_obj=payload)
        if r.value is None:
            rows.append({"case": f.stem, "outcome": "UNDEFINED"})
            failures.append(f"{f.stem}: no decision (undefined result)")
            continue
        got = {"outcome": r.value["outcome"], "band": r.value["risk"]["band"],
               "cdd_level": r.value["risk"]["cdd_level"],
               "reasons_include": r.value["reasons"][:3]}
        want = expected.get(f.stem)
        drift = [k for k in ("outcome", "band", "cdd_level") if want and got[k] != want[k]]
        if want is None:
            drift = ["unpinned"]
        if drift:
            failures.append(f"{f.stem}: " + ", ".join(
                f"{k} {want.get(k) if want else '?'} -> {got.get(k)}" for k in drift))
        rows.append({"case": f.stem, **got, "drift": drift})

    ledger.test_run(scope="decision corpus", passed=len(rows) - len(failures),
                    failed=len(failures), detail="; ".join(failures[:5]))
    width = max(len(r["case"]) for r in rows)
    for r in rows:
        mark = "  " if not r.get("drift") else "!!"
        print(f"{mark} {r['case']:<{width}}  {r['outcome']:<24} {r.get('band',''):<10} "
              f"{r.get('cdd_level',''):<5} {', '.join(r.get('reasons_include', []))}")
    print(f"\n  {len(rows) - len(failures)}/{len(rows)} cases match the pinned baseline")
    for f in failures:
        print(f"  !! {f}")
    return 0 if not failures else 1


def cmd_ledger(args) -> int:
    opa, ledger = _ctx()
    ok, head, broken = ledger.verify_chain()
    recs = list(ledger.records())
    types: dict[str, int] = {}
    for r in recs:
        types[r["record_type"]] = types.get(r["record_type"], 0) + 1
    print(json.dumps({"chain_valid": ok, "head": head, "broken_at": broken,
                      "records": len(recs), "by_type": types}, indent=2))
    return 0 if ok else 2


def cmd_replay(args) -> int:
    """Reproducibility check: recompute a decision from its input and compare it with the record the
    ledger holds. This is the only way to prove the ledger is a record of *this* policy, not of a
    policy that used to be deployed."""
    opa, ledger = _ctx()
    payload = json.loads(Path(args.input).read_text())
    result = opa.eval("data.agent.decision.result", data_paths=[ROOT / "policy"], input_obj=payload)
    fresh = result.value
    if fresh is None:
        print(json.dumps({"error": "policy returned no decision"}, indent=2))
        return 2
    stored = [r for r in ledger.records()
              if r.get("record_type") == "DECISION" and r.get("decision_id") == fresh["decision_id"]]
    if not stored:
        print(json.dumps({"reproduced": False,
                          "reason": "no ledger record for this decision_id",
                          "decision_id": fresh["decision_id"]}, indent=2))
        return 3
    rec = stored[-1]
    fields = ("outcome", "reasons", "route")
    diffs = [f for f in fields if rec.get(f) != fresh.get(f)]
    print(json.dumps({"reproduced": not diffs, "decision_id": fresh["decision_id"],
                      "evidence_id": rec["evidence_id"],
                      "bundle_revision": (rec.get("bundle") or {}).get("revision"),
                      "policy_version": (rec.get("bundle") or {}).get("policy_version"),
                      "compared_fields": list(fields), "differences": diffs}, indent=2))
    return 0 if not diffs else 2


def main() -> int:
    p = argparse.ArgumentParser(prog="axiom-policy", description="AXIOM-POLICY agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decide", help="evaluate one policy decision")
    d.add_argument("--input", required=True)
    d.add_argument("--explain", action="store_true")
    d.set_defaults(func=cmd_decide)

    a = sub.add_parser("authority", help="authorize an agent action")
    a.add_argument("--action", required=True)
    a.add_argument("--actor", default="ATLAS-CDD")
    a.set_defaults(func=cmd_authority)

    c = sub.add_parser("check", help="compile policy under restricted capabilities")
    c.add_argument("--strict", action="store_true")
    c.set_defaults(func=cmd_check)

    t = sub.add_parser("test", help="run Rego tests")
    t.add_argument("--verbose", action="store_true")
    t.set_defaults(func=cmd_test)

    b = sub.add_parser("build", help="build + verify + stage a bundle")
    b.add_argument("--revision", required=True)
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="verify a built bundle")
    v.add_argument("--revision", required=True)
    v.set_defaults(func=cmd_verify)

    au = sub.add_parser("author", help="turn SOP clauses into a draft policy change")
    au.add_argument("--doc", required=True)
    au.add_argument("--proposal", required=True)
    au.add_argument("--change", required=True)
    au.set_defaults(func=cmd_author)

    co = sub.add_parser("corpus", help="run the decision corpus")
    co.set_defaults(func=cmd_corpus)

    rp = sub.add_parser("replay", help="recompute a stored decision and compare (reproducibility)")
    rp.add_argument("--input", required=True)
    rp.set_defaults(func=cmd_replay)

    le = sub.add_parser("ledger", help="verify the decision evidence chain")
    le.set_defaults(func=cmd_ledger)

    args = p.parse_args()
    try:
        return args.func(args)
    except OPAError as exc:
        print(json.dumps({"opa_error": str(exc), "stderr": exc.stderr}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
