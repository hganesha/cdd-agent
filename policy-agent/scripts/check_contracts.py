#!/usr/bin/env python3
"""Contract checks for AXIOM-POLICY — what `make check` runs after the tests.

  1. every decision / authority / manifest / proposal artefact validates against its schema
  2. the prompt + data.json pins match prompts/pins.json
  3. the corpus baseline matches its pinned expectations
  4. the evidence chain verifies
  5. the authority projection has not drifted from the Rego
Exit 0 only if all five hold.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jsonschema import Draft202012Validator        # noqa: E402

from axiom.bundle import PolicyBundleService        # noqa: E402
from axiom.evidence import DecisionLedger           # noqa: E402
from axiom.opa import OPAClient                     # noqa: E402

fails: list[str] = []


def ok(label, detail=""):
    print(f"  ok    {label}{(' — ' + detail) if detail else ''}")


def bad(label, detail):
    fails.append(label)
    print(f"  FAIL  {label} — {detail}")


def validate(instance, schema_name, label):
    schema = json.loads((ROOT / "schemas" / schema_name).read_text())
    errors = [f"{list(e.path)}: {e.message}" for e in Draft202012Validator(schema).iter_errors(instance)]
    ok(label) if not errors else bad(label, "; ".join(errors[:4]))


def check_artefacts():
    print("artefacts")
    last = ROOT / "out" / "last_decision.json"
    if last.exists():
        d = json.loads(last.read_text())
        d.pop("evidence_id", None)
        validate(d, "decision.schema.json", "last decision")
    for m in sorted((ROOT / "out" / "bundles").glob("*.manifest.json")):
        validate(json.loads(m.read_text()), "bundle-manifest.schema.json", f"manifest {m.name}")
    for p in sorted((ROOT / "proposals").glob("P-*.json")):
        validate(json.loads(p.read_text()), "policy-proposal.schema.json", f"proposal {p.name}")


def check_pins():
    print("pins")
    pins = json.loads((ROOT / "prompts" / "pins.json").read_text())
    for rel, digest in pins["files"].items():
        actual = "sha256:" + hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        ok(f"{rel} pinned") if actual == digest else bad(
            rel, f"changed since pin ({digest[:19]}… → {actual[:19]}…); re-pin and re-run the suite")


def check_corpus():
    print("corpus")
    opa = OPAClient(binary=str(ROOT / "bin" / "opa") if (ROOT / "bin" / "opa").exists() else None)
    expected = json.loads((ROOT / "corpus" / "expected.json").read_text())["cases"]
    cases = sorted((ROOT / "corpus" / "decisions").glob("*.json"))
    mismatches = []
    for f in cases:
        res = opa.eval("data.agent.decision.result", data_paths=[ROOT / "policy"],
                       input_obj=json.loads(f.read_text())).value
        want = expected.get(f.stem)
        if res is None or want is None:
            mismatches.append(f"{f.stem}: {'no decision' if res is None else 'unpinned'}")
            continue
        got = {"outcome": res["outcome"], "band": res["risk"]["band"],
               "cdd_level": res["risk"]["cdd_level"]}
        if any(got[k] != want[k] for k in got):
            mismatches.append(f"{f.stem}: {want} != {got}")
    ok(f"{len(cases) - len(mismatches)}/{len(cases)} cases match the pinned baseline") if not mismatches \
        else bad("corpus baseline", "; ".join(mismatches[:4]))


def check_ledger():
    print("evidence")
    path = ROOT / "out" / "decision_evidence.jsonl"
    if not path.exists():
        bad("ledger", "out/decision_evidence.jsonl missing")
        return
    valid, head, broken = DecisionLedger(path, agent_id="AXIOM-POLICY").verify_chain()
    ok(f"chain valid over {len(list(DecisionLedger(path, agent_id='AXIOM-POLICY').records()))} records",
       head[:18] + "…") if valid else bad("chain", f"broken at seq {broken}")


def check_authority():
    print("authority projection")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_authority.py")],
                       capture_output=True, text=True)
    ok("config/authority.yaml agrees with agent/authority.rego") if r.returncode == 0 else bad(
        "authority drift", (r.stdout + r.stderr).strip().splitlines()[-1] if r.stdout or r.stderr else "")


def main():
    print(f"AXIOM-POLICY contract check · {ROOT}\n")
    for fn in (check_artefacts, check_pins, check_corpus, check_ledger, check_authority):
        fn()
    print()
    if fails:
        print(f"FAILED — {len(fails)} problem(s): {', '.join(fails)}")
        return 1
    print("all contracts hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
