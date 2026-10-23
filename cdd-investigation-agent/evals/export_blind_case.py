#!/usr/bin/env python3
"""Copy one synthetic case's inputs into a gold-free agent workspace."""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "golden"


def export(case_id: str, out: pathlib.Path, *, force: bool = False) -> None:
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    known = {entry["case_id"] for entry in manifest["cases"]}
    if case_id not in known:
        raise ValueError(f"unknown case ID: {case_id}")
    out = out.resolve()
    if out.is_relative_to(CORPUS) or ROOT.is_relative_to(out):
        raise ValueError("destination must be outside the corpus and cannot contain the repository")
    if out.exists():
        if not force:
            raise FileExistsError(f"{out} exists; use --force to replace")
        if not (out / ".blind_case_export").is_file():
            raise ValueError("refusing to replace a directory not created by this exporter")
        shutil.rmtree(out)
    source = CORPUS / case_id
    out.mkdir(parents=True)
    for name in ("case.json", "bundle.json", "connector_results.json", "search_request.json"):
        shutil.copy2(source / name, out / name)
    shutil.copytree(source / "pages", out / "pages")
    shutil.copytree(source / "search_mock", out / "search_mock")
    shutil.copy2(ROOT / "config" / "policy.yaml", out / "policy.yaml")
    case_path = out / "case.json"
    case = json.loads(case_path.read_text())
    case["bundle_ref"]["manifest_uri"] = "bundle.json"
    case_path.write_text(json.dumps(case, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    (out / ".blind_case_export").write_text(case_id + "\n")
    if any(path.name in {"gold.json", "manifest.json"} for path in out.rglob("*")):
        raise RuntimeError("answer key leaked into blind export")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id", help="e.g. CASE-700001")
    parser.add_argument("out", type=pathlib.Path, help="new destination directory")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    export(args.case_id, args.out.resolve(), force=args.force)
    print(f"exported {args.case_id} to {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
