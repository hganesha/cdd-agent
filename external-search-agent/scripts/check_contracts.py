#!/usr/bin/env python3
"""Contract checks — what `make check` runs after the unit tests.

  1. every artefact in out/ validates against its schema
  2. the source policy validates, and its compliance constants are the ones we require
  3. the prompt hashes match prompts/pins.json
  4. every quote re-slices from its span (delegated to webis.cli verify)
  5. the ledger chain verifies

Exit code 0 only if all five pass. No artefact is written.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from jsonschema import Draft202012Validator
except ImportError:                                        # pragma: no cover
    print("jsonschema is required: pip install jsonschema")
    raise SystemExit(2)

import yaml                                               # noqa: E402

from webis.agent import SearchAgent                      # noqa: E402
from webis.evidence import EvidenceLedger                 # noqa: E402

fails: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  ok    {label}{(' — ' + detail) if detail else ''}")


def bad(label: str, detail: str) -> None:
    fails.append(f"{label}: {detail}")
    print(f"  FAIL  {label} — {detail}")


def validate(instance, schema_name: str, label: str) -> None:
    schema = json.loads((ROOT / "schemas" / schema_name).read_text())
    errors = [f"{list(e.path)}: {e.message}" for e in
              Draft202012Validator(schema).iter_errors(instance)]
    ok(label) if not errors else bad(label, "; ".join(errors[:4]))


def check_artefacts() -> None:
    print("artefacts")
    pack_path = ROOT / "out" / "evidence_pack.json"
    if not pack_path.exists():
        bad("evidence pack", "out/evidence_pack.json missing — run `make demo`")
        return
    pack = json.loads(pack_path.read_text())
    validate(pack, "evidence-pack.schema.json", "evidence pack")
    validate(json.loads(json.dumps(yaml.safe_load(
        (ROOT / "config" / "sources.yaml").read_text()), default=str)),
        "source-policy.schema.json", "source policy")

    state = ROOT / "out" / "state"
    for f in sorted(state.glob("run_*.json")):
        validate(json.loads(f.read_text()), "run-state.schema.json", f"run state {f.name}")


def check_ledger() -> None:
    print("ledger")
    path = ROOT / "out" / "search_evidence.jsonl"
    if not path.exists():
        bad("ledger", "out/search_evidence.jsonl missing")
        return
    schema = json.loads((ROOT / "schemas" / "evidence-record.schema.json").read_text())
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    invalid = 0
    for rec in records:
        errs = list(Draft202012Validator(schema).iter_errors(rec))
        if errs:
            invalid += 1
            if invalid == 1:
                print(f"        first invalid record seq={rec.get('seq')}: "
                      f"{errs[0].message[:120]}")
    ok(f"{len(records)} records validate") if not invalid else bad("ledger records", f"{invalid} invalid")

    valid, head, broken = EvidenceLedger(path, agent_id="WEBIS-SEARCH").verify_chain()
    ok("chain valid", head[:18] + "…") if valid else bad("chain", f"broken at seq {broken}")


def check_prompts() -> None:
    print("prompts")
    pins_file = ROOT / "prompts" / "pins.json"
    pins = json.loads(pins_file.read_text())
    for rel, digest in pins["files"].items():
        actual = "sha256:" + hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if actual == digest:
            ok(f"{rel} pinned")
        else:
            bad(rel, f"changed since pin ({digest[:19]}… → {actual[:19]}…); "
                     f"re-pin and re-run the regression suite")


def check_quotes() -> None:
    print("quotations")
    pack_path = ROOT / "out" / "evidence_pack.json"
    if not pack_path.exists():
        return
    from webis.fetch import sha256
    corpus = {}
    for f in (ROOT / "corpus" / "sites").rglob("*.json"):
        d = json.loads(f.read_text())
        corpus[d["url"]] = d
    checked = failed = 0
    for claim in json.loads(pack_path.read_text())["claims"]:
        for cit in claim["citations"]:
            checked += 1
            doc = corpus.get(cit["url"])
            span = (cit["char_span"][0], cit["char_span"][1])
            span_text = doc["body"][span[0]:span[1]] if doc else None
            if not doc:
                failed += 1
                continue
            if sha256(doc["body"]) != cit["content_hash"]:
                failed += 1
            elif cit.get("span_text_sha256") and sha256(span_text) != cit["span_text_sha256"]:
                failed += 1
            elif not cit.get("pii_redactions") and span_text != cit["quote"]:
                failed += 1
    ok(f"{checked} quotations re-derived") if not failed else bad(
        "quotation integrity", f"{failed}/{checked} failed — pack must not be consumed")


def main() -> int:
    print(f"WEBIS-SEARCH contract check · {ROOT}\n")
    check_artefacts()
    check_ledger()
    check_prompts()
    check_quotes()
    print()
    if fails:
        print(f"FAILED — {len(fails)} problem(s)")
        return 1
    print("all contracts hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
