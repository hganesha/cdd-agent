#!/usr/bin/env python3
"""Run the sibling WEBIS-SEARCH agent on one golden case's mock web corpus."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEARCH_ROOT = ROOT.parent / "external-search-agent"


def run(case_id: str, out: pathlib.Path) -> dict:
    if not (SEARCH_ROOT / "src" / "webis" / "agent.py").exists():
        raise FileNotFoundError(f"sibling external-search-agent not found at {SEARCH_ROOT}")
    case = ROOT / "evals" / "golden" / case_id
    request = json.loads((case / "search_request.json").read_text())
    if request["case_id"] != case_id:
        raise ValueError("search request does not match case ID")
    sys.path.insert(0, str(SEARCH_ROOT / "src"))
    from webis.agent import SearchAgent  # noqa: E402
    from webis.fetch import Fetcher, SourcePolicy, sha256  # noqa: E402

    source_cfg = yaml.safe_load((SEARCH_ROOT / "config" / "sources.yaml").read_text())
    budget = yaml.safe_load((SEARCH_ROOT / "config" / "budget.yaml").read_text())["budget"]
    request_schema = json.loads((SEARCH_ROOT / "schemas" / "search-request.schema.json").read_text())
    # The sibling schema predates six-digit golden case IDs. Enforce its other constraints
    # while accepting the shared CASE-<digits> convention used by the CDD input schema.
    request_schema["properties"]["case_id"]["pattern"] = r"^CASE-[0-9]{4,8}$"
    Draft202012Validator(request_schema).validate(request)
    policy = SourcePolicy(source_cfg)
    fetcher = Fetcher(policy, corpus=case / "search_mock" / "sites")
    agent = SearchAgent(fetcher, policy, budget=budget)
    result = agent.run(request)
    pack = agent.pack(result)
    pack["pack_id"] = "PACK-" + sha256(json.dumps(pack, sort_keys=True))[7:19]
    pack["source_policy_version"] = source_cfg["version"]
    pack_schema = json.loads((SEARCH_ROOT / "schemas" / "evidence-pack.schema.json").read_text())
    Draft202012Validator(pack_schema).validate(pack)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id", help="e.g. CASE-700141")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()
    out = args.out or ROOT / "out" / "golden-search" / args.case_id / "evidence_pack.json"
    pack = run(args.case_id, out.resolve())
    print(json.dumps({"case_id": args.case_id, "status": pack["status"],
                      "claims": len(pack["claims"]),
                      "quarantined": len(pack["quarantined_sources"]),
                      "refused": len(pack["refused_sources"]),
                      "out": str(out.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
