#!/usr/bin/env python3
"""Assert that config/authority.yaml (the human-readable projection) agrees with
policy/agent/authority.rego (the source of truth).

A projection that drifts from the Rego is worse than no projection: reviewers would sign off on a
table that is not the one being enforced. Run by `make check-authority`; exits non-zero on drift.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPA = ROOT / "bin" / "opa" if (ROOT / "bin" / "opa").exists() else "opa"


def query(expr: str):
    out = subprocess.run([str(OPA), "eval", "-d", str(ROOT / "policy"), "-f", "json", expr],
                         capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        print(out.stderr.strip(), file=sys.stderr)
        raise SystemExit(2)
    return json.loads(out.stdout)["result"][0]["expressions"][0]["value"]


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "authority.yaml").read_text())
    problems: list[str] = []

    prohibited = set(query("data.agent.authority.prohibited_actions"))
    gated = query("data.agent.authority.gated_actions")
    allowed = set(query("data.agent.authority.allowed_actions"))

    y_prohibited = set(cfg["prohibited"])
    y_gated = {k: {"approver": v["approver"], "ttl_hours": v["ttl_hours"]}
               for k, v in cfg["gated"].items()}
    y_allowed = set(cfg["allowed"])

    for label, rego, yml in (("prohibited", prohibited, y_prohibited),
                             ("allowed", allowed, y_allowed)):
        if rego != yml:
            problems.append(f"{label}: only-in-rego={sorted(rego - yml)} only-in-yaml={sorted(yml - rego)}")
    if gated != y_gated:
        problems.append(f"gated: only-in-rego={sorted(set(gated) - set(y_gated))} "
                        f"only-in-yaml={sorted(set(y_gated) - set(gated))} "
                        f"differing={sorted(k for k in set(gated) & set(y_gated) if gated[k] != y_gated[k])}")

    # the three sets must be disjoint, or an action would have two answers
    for a, b, la, lb in ((prohibited, allowed, "prohibited", "allowed"),
                         (prohibited, set(gated), "prohibited", "gated"),
                         (allowed, set(gated), "allowed", "gated")):
        if a & b:
            problems.append(f"{la} and {lb} overlap: {sorted(a & b)}")

    print(json.dumps({"prohibited": len(prohibited), "gated": len(gated), "allowed": len(allowed),
                      "default": "denied", "drift": problems}, indent=2))
    if problems:
        print("authority projection has drifted from the Rego", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
