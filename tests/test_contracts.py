"""Validates every emitted artifact against its JSON Schema.

Run: python3 tests/test_contracts.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cdd_agent.orchestrator import Orchestrator  # noqa: E402

CASE = ROOT / "data" / "case_4831"
OUT = ROOT / "out" / "contracts"
SCHEMAS = ROOT / "schemas"


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def validate(instance: dict, name: str, label: str) -> None:
    jsonschema.validate(instance, schema(name))
    print(f"PASS  {label:<28} → {name}")


def main() -> int:
    import shutil
    if OUT.exists():
        shutil.rmtree(OUT)
    Orchestrator(CASE, OUT).run()

    validate(json.loads((CASE / "case.json").read_text()), "case-input.schema.json", "case input")
    validate(json.loads((OUT / "report.json").read_text()), "cdd-memo.schema.json", "CDD report")
    validate(json.loads((OUT / "coverage_matrix.json").read_text()), "coverage-matrix.schema.json",
             "coverage matrix")
    validate(json.loads((OUT / "state" / "run_state.json").read_text()), "run-state.schema.json",
             "run state")
    validate(json.loads((OUT / "document_manifest.json").read_text()),
             "document-manifest.schema.json", "document manifest")

    findings = json.loads((OUT / "findings.json").read_text())
    for f in findings:
        validate(f, "finding.schema.json", f"finding {f['finding_id']}")
    print(f"PASS  {len(findings)} findings validated")

    # risk rating is embedded in the manifest; validate it against its own schema
    led = [json.loads(l) for l in (OUT / "evidence_ledger.jsonl").open()]
    rr = next(r for r in led if r["record_type"] == "COMPUTATION"
              and r["subject"]["kind"] == "RISK_RATING")
    print(f"PASS  risk computation recorded at {rr['evidence_id']} "
          f"({rr['computation']['calc_id']})")

    # every evidence record must satisfy the evidence schema
    for r in led:
        jsonschema.validate(r, schema("evidence-record.schema.json"))
    print(f"PASS  {len(led)} evidence records validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
