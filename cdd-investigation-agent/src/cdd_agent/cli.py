#!/usr/bin/env python3
"""CLI for the ATLAS-CDD reference implementation.

  python -m cdd_agent.cli run            # full run
  python -m cdd_agent.cli run --kill-at 8 # crash after 8 units (resumability demo)
  python -m cdd_agent.cli resume         # continue from the last checkpoint
  python -m cdd_agent.cli verify         # independent evidence-chain verification
  python -m cdd_agent.cli clean
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cdd_agent.orchestrator import Orchestrator, quick_verify  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
CASE = ROOT / "data" / "case_4831"
OUT = ROOT / "out"


def cmd_run(args) -> int:
    if args.clean and OUT.exists():
        shutil.rmtree(OUT)
    orch = Orchestrator(CASE, OUT, budget_usd=args.budget, kill_at_unit=args.kill_at)
    manifest = orch.run()
    print("\n── Run manifest ──")
    for k in ("run_id", "status", "coverage", "risk", "critic", "coverage_matrix",
              "required_gates", "counts", "cost"):
        if k in manifest:
            print(f"  {k}: {json.dumps(manifest[k])}")
    print(f"\n  evidence chain: {'VALID' if manifest.get('evidence_ledger', {}).get('chain_valid') else 'INVALID'}")
    print(f"  dossier:        {OUT / 'CDD_dossier_CASE-4831.md'}")
    return 0


def cmd_resume(args) -> int:
    if not (OUT / "state" / "run_state.json").exists():
        print("no checkpoint found — run `run` first")
        return 1
    before = quick_verify(OUT) if (OUT / "evidence_ledger.jsonl").exists() else {"records": 0}
    orch = Orchestrator(CASE, OUT, budget_usd=args.budget)
    manifest = orch.run()
    after = quick_verify(OUT)
    print(f"\n  resumed run: {manifest['run_id']}")
    print(f"  evidence records: before={before['records']} after={after['records']} "
          f"delta={after['records'] - before['records']}")
    print(f"  duplicate-content records in whole ledger: {after['duplicate_content_records']}")
    print(f"  chain: {'VALID' if after['chain_valid'] else 'INVALID'}")
    print(f"  pages: {manifest.get('coverage', {})}")
    return 0


def cmd_verify(args) -> int:
    if not (OUT / "evidence_ledger.jsonl").exists():
        print("no ledger found")
        return 1
    v = quick_verify(OUT)
    print(json.dumps(v, indent=2))
    return 0 if v["chain_valid"] else 2


def cmd_clean(args) -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    print(f"removed {OUT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="ATLAS-CDD reference implementation")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the investigation pipeline")
    r.add_argument("--budget", type=float, default=None, help="override USD budget")
    r.add_argument("--kill-at", type=int, default=None, help="simulate a crash after N units")
    r.add_argument("--clean", action="store_true", help="wipe out/ before running")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="resume from the last checkpoint")
    s.add_argument("--budget", type=float, default=None)
    s.set_defaults(func=cmd_resume)

    v = sub.add_parser("verify", help="verify the evidence hash chain")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("clean", help="remove run artefacts")
    c.set_defaults(func=cmd_clean)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
