#!/usr/bin/env python3
"""Contract checks for the OPS case-operations package.

Five things are checked, in the order in which a failure would hurt:

  1. every JSON Schema in schemas/ is itself valid
  2. every artefact in out/ matches its schema
  3. every prompt file still hashes to its pin in prompts/pins.json
  4. the evidence ledger's hash chain verifies, and record types are from the closed set
  5. the policy pack the agents call still answers the two rules the decision agent depends on

Exit code 0 only if all of it holds. Run via `make contracts`.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opsagents.common.ledger import OpsLedger                      # noqa: E402
from opsagents.common.policy import PolicyEngine                   # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]

try:
    from jsonschema import Draft202012Validator
except ImportError:                                   # pragma: no cover
    Draft202012Validator = None


def ok(msg: str) -> None:
    CHECKS[0] += 1
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    CHECKS[0] += 1
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def validate(instance, schema_name: str, label: str) -> None:
    schema = json.loads((ROOT / "schemas" / schema_name).read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        bad(f"{label}: {list(errors[0].path)} {errors[0].message[:120]}")
    else:
        ok(label)


def main() -> int:
    print("schemas")
    if Draft202012Validator is None:
        bad("jsonschema is not installed — schema contracts cannot be checked")
    else:
        for f in sorted((ROOT / "schemas").glob("*.json")):
            try:
                Draft202012Validator.check_schema(json.loads(f.read_text()))
                ok(f"schema {f.name} is a valid JSON Schema")
            except Exception as exc:                          # noqa: BLE001
                bad(f"schema {f.name}: {exc}")

    print("artefacts")
    artefacts = [
        ("profiles", "customer-profile.schema.json"),
        ("dispositions", "disposition.schema.json"),
    ]
    found = False
    for folder, schema in artefacts:
        for f in sorted((ROOT / "out" / folder).glob("*.json")):
            found = True
            validate(json.loads(f.read_text()), schema, f"{folder}/{f.name}")
    for f in sorted((ROOT / "out" / "rfi").glob("*.json")):
        found = True
        validate(json.loads(f.read_text()), "rfi.schema.json", f"rfi/{f.name}")
    for f in sorted((ROOT / "out" / "state").glob("*-profile.json")):
        found = True
        validate(json.loads(f.read_text()), "ops-run-state.schema.json", f"state/{f.name}")
    if not found:
        bad("no artefacts in out/ — run `make run` first")

    print("prompts")
    pins = json.loads((ROOT / "prompts" / "pins.json").read_text())["files"]
    for rel, pinned in pins.items():
        actual = "sha256:" + hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if actual == pinned:
            ok(f"{rel} pinned")
        else:
            bad(f"{rel} changed without re-pinning ({actual[:19]}… != {pinned[:19]}…)")

    print("evidence")
    ledger = OpsLedger(ROOT / "out" / "ops_evidence.jsonl", agent_id="OPS")
    if not ledger.path.exists():
        bad("no evidence ledger — run `make run` first")
    else:
        good, head, broken = ledger.verify_chain()
        if good:
            ok(f"chain valid over {len(list(ledger.records()))} records — {head[:18]}…")
        else:
            bad(f"chain broken at record {broken}")
        allowed = {"API_CALL", "PROFILE", "PROFILE_DELTA", "VERIFICATION", "RFI", "DISPOSITION",
                   "EVENT"}
        odd = {r["record_type"] for r in ledger.records()} - allowed
        if odd:
            bad(f"unknown record types in the ledger: {sorted(odd)}")
        else:
            ok("record types are from the closed set")
        ids = [r["evidence_id"] for r in ledger.records()]
        if len(ids) != len(set(ids)):
            bad("duplicate evidence ids in the ledger")
        else:
            ok("evidence ids are unique")

    print("policy service")
    try:
        policy = PolicyEngine()
        ok(f"policy pack {policy.policy_version} loads")
        for band in ("LOW", "MEDIUM", "HIGH", "VERY_HIGH"):
            ad = policy.dispose(band=band, recommendation="PROCEED", required_gates=[],
                                critic_status="PASS")
            if ad["agent_may_finalise"] is not False:
                bad(f"policy admits agent finalisation on band {band}")
        ok("disposition never allows an agent to finalise")
        auth = policy.authority("case.finalise")
        if auth.get("decision") != "denied":
            bad("case.finalise is not denied — the authority projection has drifted")
        else:
            ok("case.finalise is denied by the policy service")
    except Exception as exc:                                  # noqa: BLE001
        bad(f"policy service unavailable: {exc}")

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} problem(s) out of {CHECKS[0]} checks:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all contracts hold ({CHECKS[0]} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
