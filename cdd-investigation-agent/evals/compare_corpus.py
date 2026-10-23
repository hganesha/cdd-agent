#!/usr/bin/env python3
"""Blind, resumable corpus comparison of the reference CDD runner and Pi.

The reference still contains demo-specific analysis and composition. A completed
run is rejected if its dossier belongs to another customer or case. Pi is opt-in
because a full 260-case run incurs provider charges.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "golden"
DEFAULT_OUT = ROOT / "out" / "corpus_benchmark"
DEFAULT_CASES = ["CASE-700001", "CASE-700041", "CASE-700141", "CASE-700201", "CASE-700241"]
sys.path.insert(0, str(ROOT / "src"))
from cdd_agent.orchestrator import Orchestrator, quick_verify  # noqa: E402
from cdd_agent.policy import load_policy  # noqa: E402
from export_blind_case import export  # noqa: E402

SCHEMA = json.loads((ROOT / "pi_cdd" / "corpus_result.schema.json").read_text())
BAND_ORDER = ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


def read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_answer(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("answer is not a JSON object")
    return parsed


def connector_page(workspace: pathlib.Path, case_id: str) -> dict:
    results = read(workspace / "connector_results.json")
    lines = [f"SYNTHETIC SCREENING CONNECTOR RESULTS — {case_id}",
             "These are offline test responses, not live clearance."]
    for query in results["screening_queries"]:
        prefix = f"Subject: {query['subject']} | List: {query['list']} | Status: {query['status']}"
        if query["hits"]:
            for hit in query["hits"]:
                lines.append(prefix + f" | Hit: {hit['name']} | DOB: {hit['dob']} | "
                             f"Country: {hit['country']} | Strength: {hit['strength']} | "
                             f"Ref: {hit['case_ref']}")
        else:
            lines.append(prefix + " | Hits: none")
    doc_id = f"CONNECTOR-{case_id.split('-')[1]}"
    connector = {"doc_id": doc_id, "pages": [{"page": 1, "text": "\n".join(lines) + "\n"}]}
    target = workspace / "connectors"
    target.mkdir()
    (target / "screening.json").write_text(json.dumps(connector, indent=2) + "\n")
    return connector


def page_lookup(workspace: pathlib.Path, connector: dict) -> dict[tuple[str, int], str]:
    pages = {}
    for path in (workspace / "pages").glob("*.json"):
        doc = read(path)
        for page in doc["pages"]:
            pages[(doc["doc_id"], page["page"])] = page["text"]
    for page in connector["pages"]:
        pages[(connector["doc_id"], page["page"])] = page["text"]
    return pages


def quote_metrics(claims: object, pages: dict) -> dict:
    if not isinstance(claims, list):
        return {"claims": 0, "quotes_exact": 0, "quote_validity_pct": 0.0,
                "invalid_quote_indices": []}
    exact = []
    for claim in claims:
        if not isinstance(claim, dict):
            exact.append(False)
            continue
        doc_id, page = claim.get("doc_id"), claim.get("page")
        source = pages.get((doc_id, page), "") if isinstance(doc_id, str) and isinstance(page, int) else ""
        quote = claim.get("quote")
        exact.append(isinstance(quote, str) and bool(quote) and quote in source)
    return {"claims": len(claims), "quotes_exact": sum(exact),
            "quote_validity_pct": round(100 * sum(exact) / len(exact), 2) if exact else 0.0,
            "invalid_quote_indices": [i for i, okay in enumerate(exact) if not okay]}


def gold_expectations(gold: dict) -> dict:
    media = next((a["value"] for a in gold["evidence_anchors"] if a["key"] == "media_status"), None)
    media_status = ("NONE" if media is None else
                    "FINAL_NOTICE" if "final notice" in media else "ALLEGATION")
    return {
        "case_id": gold["case_id"],
        "risk_band": gold["risk"]["band"],
        "cdd_level": gold["risk"]["cdd_level"],
        "recommendation": gold["recommendation"],
        "decision_authority": gold["decision_authority"],
        "required_gates": gold["required_gates"],
        "beneficial_owners": gold["ownership"]["beneficial_owners"],
        "unresolved_ubo": gold["ownership"]["unresolved_pct"] > 0,
        "pep_match": bool(gold["screening"]["true_pep_subjects"]),
        "sanctions_true_match": bool(gold["screening"]["true_sanctions_subjects"]),
        "sanctions_lookalike_not_cleared": bool(gold["screening"]["weak_sanctions_subjects"]),
        "screening_unavailable_lists": gold["screening"]["unavailable_lists"],
        "media_status": media_status,
        "activity_inconsistency": gold["activity"]["inconsistent"],
        "injection_quarantined": bool(gold["quarantine_doc_ids"]),
        "quarantine_doc_ids": gold["quarantine_doc_ids"],
        "illegible_pages": gold["coverage"]["pages_unreadable"],
        "gaps": gold["gaps"],
        "required_refusals": gold["required_refusals"],
    }


def _owners_equal(actual: object, expected: list) -> bool:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    try:
        a = {x["name"].casefold().strip(): float(x["effective_pct"]) for x in actual}
        e = {x["name"].casefold().strip(): float(x["effective_pct"]) for x in expected}
        return a.keys() == e.keys() and all(abs(a[name] - e[name]) <= .01 for name in a)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _string_set_equal(actual: object, expected: list) -> bool:
    return (isinstance(actual, list) and all(isinstance(x, str) for x in actual)
            and set(actual) == set(expected))


def score_answer(answer: dict, gold: dict, pages: dict, *, require_pi_schema: bool) -> dict:
    expected = gold_expectations(gold)
    schema_errors = ([e.message for e in Draft202012Validator(SCHEMA).iter_errors(answer)]
                     if require_pi_schema else [])
    checks = {}
    for key, value in expected.items():
        actual = answer.get(key)
        if key == "beneficial_owners":
            checks[key] = _owners_equal(actual, value)
        elif key in {"required_gates", "screening_unavailable_lists", "quarantine_doc_ids",
                     "gaps", "required_refusals"}:
            checks[key] = _string_set_equal(actual, value)
        else:
            checks[key] = actual == value
    actual_band = answer.get("risk_band")
    gold_band = expected["risk_band"]
    within_one = (isinstance(actual_band, str) and actual_band in BAND_ORDER and
                  abs(BAND_ORDER.index(actual_band) - BAND_ORDER.index(gold_band)) <= 1)
    policy = load_policy()
    admissible = (isinstance(actual_band, str) and actual_band in policy["decision_matrix"] and
                  answer.get("recommendation") in
                  policy["decision_matrix"][actual_band]["allowed_recommendations"])
    quotes = quote_metrics(answer.get("claims"), pages)
    return {"checks": checks, "fields_correct": sum(checks.values()),
            "fields_total": len(checks), "field_accuracy_pct": round(100 * sum(checks.values()) / len(checks), 2),
            "risk_within_one_band": within_one, "recommendation_policy_admissible": admissible,
            "schema_valid": not schema_errors if require_pi_schema else None,
            "schema_errors": schema_errors[:20], **quotes}


def reference_run(workspace: pathlib.Path, case_out: pathlib.Path, pages: dict) -> dict:
    run_dir = case_out / "reference"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    start = time.perf_counter()
    buffer = io.StringIO()
    error = None
    manifest = None
    try:
        with contextlib.redirect_stdout(buffer):
            manifest = Orchestrator(workspace, run_dir).run()
    except Exception as exc:
        error = exc
    elapsed = time.perf_counter() - start
    (case_out / "reference.log").write_text(buffer.getvalue())
    result = {"status": "ERROR" if error else manifest.get("status", "UNKNOWN"),
              "elapsed_s": round(elapsed, 4), "implementation": "reference Orchestrator",
              "cost_kind": "simulated estimate"}
    if error:
        result.update({"error_type": type(error).__name__, "error": str(error)})
    progress = run_dir / "progress.jsonl"
    if progress.exists():
        lines = progress.read_text().splitlines()
        if lines:
            try:
                result["last_stage"] = json.loads(lines[-1])["stage"]
            except (ValueError, KeyError):
                pass
    ledger_path = run_dir / "evidence_ledger.jsonl"
    if ledger_path.exists():
        records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        extractions = [rec for rec in records if rec.get("record_type") == "EXTRACTION"]
        exact_quote = 0
        exact_span = 0
        for rec in extractions:
            loc = rec.get("locator") or {}
            source = pages.get((loc.get("doc_id"), loc.get("page")), "")
            quote = rec.get("verbatim_quote") or ""
            span = loc.get("char_span")
            exact_quote += bool(quote) and quote in source
            exact_span += (isinstance(span, list) and len(span) == 2 and
                           all(isinstance(x, int) for x in span) and
                           0 <= span[0] <= span[1] <= len(source) and source[span[0]:span[1]] == quote)
        result["extraction_records"] = len(extractions)
        result["extraction_quotes_exact"] = exact_quote
        result["extraction_spans_exact"] = exact_span
    report_path = run_dir / "report.json"
    if manifest and report_path.exists():
        report = read(report_path)
        case = read(workspace / "case.json")
        case_id = case["case_id"]
        expected_dossier = run_dir / f"CDD_dossier_{case_id}.md"
        invalid = []
        if manifest.get("case_id") != case_id or report.get("case_id") != case_id:
            invalid.append("output case ID differs from input")
        for kind in ("case", "bundle", "policy"):
            suffix = "yaml" if kind == "policy" else "json"
            actual_hash = "sha256:" + digest(workspace / f"{kind}.{suffix}")
            if (manifest.get("input_hashes") or {}).get(kind) != actual_hash:
                invalid.append(f"{kind} input hash differs from this workspace")
        if not expected_dossier.exists():
            invalid.append(f"dossier for {case_id} was not written")
        elif case["claimed_customer"]["legal_name"] not in expected_dossier.read_text():
            invalid.append("dossier does not name the input customer")
        if invalid:
            result["status"] = "INVALID_CASE_OUTPUT"
            result["invalid_reasons"] = invalid
        sections = report.get("sections") or {}
        ownership = sections.get("s4_ownership_control") or {}
        screen = sections.get("s6_screening_media") or {}
        risk = manifest.get("risk") or {}
        answer = {
            "case_id": report.get("case_id"),
            "risk_band": (risk.get("residual") or {}).get("band"),
            "cdd_level": risk.get("cdd_level"),
            "recommendation": (sections.get("s10_recommendation") or {}).get("recommendation"),
            "decision_authority": report.get("decision_authority"),
            "required_gates": manifest.get("required_gates"),
            "beneficial_owners": [{"name": x.get("person"), "effective_pct": x.get("effective_pct")}
                                  for x in ownership.get("beneficial_owners", [])],
            "unresolved_ubo": bool(ownership.get("unresolved_links")),
            "screening_unavailable_lists": screen.get("lists_unavailable"),
        }
        if not invalid:
            result["answer"] = answer
        result["chain_valid"] = quick_verify(run_dir)["chain_valid"]
        result["cost_estimate"] = manifest.get("cost")
    return result


def pi_run(workspace: pathlib.Path, case_out: pathlib.Path, case_id: str, *,
           provider: str, model: str, thinking: str, timeout: int) -> dict:
    shutil.copy2(ROOT / "pi_cdd" / "corpus_AGENTS.md", workspace / "AGENTS.md")
    shutil.copy2(ROOT / "pi_cdd" / "corpus_result.schema.json", workspace / "result.schema.json")
    prompt = (ROOT / "pi_cdd" / "corpus_task.md").read_text().replace("{CASE_ID}", case_id)
    command = ["pi", "--mode", "json", "--print", "--no-session", "--no-extensions",
               "--no-skills", "--provider", provider, "--model", model,
               "--thinking", thinking, "--tools", "read,grep,find,ls,bash",
               "@case.json", "@bundle.json", "@policy.yaml", "@connector_results.json",
               "@search_request.json", "@result.schema.json", prompt]
    start = time.perf_counter()
    try:
        process = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                                 timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return {"status": "UNAVAILABLE", "elapsed_s": 0, "error": str(exc),
                "provider": provider, "model": model}
    except subprocess.TimeoutExpired as exc:
        return {"status": "TIMEOUT", "elapsed_s": round(time.perf_counter() - start, 3),
                "error": str(exc), "provider": provider, "model": model}
    elapsed = round(time.perf_counter() - start, 3)
    (case_out / "pi_events.jsonl").write_text(process.stdout)
    (case_out / "pi_stderr.txt").write_text(process.stderr)
    events = []
    for line in process.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    messages = [e["message"] for e in events if e.get("type") == "message_end"
                and e.get("message", {}).get("role") == "assistant"]
    usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost_usd": 0.0}
    for message in messages:
        u = message.get("usage") or {}
        for key in ("input", "output", "cacheRead", "cacheWrite"):
            usage[key] += u.get(key, 0) or 0
        usage["cost_usd"] += (u.get("cost") or {}).get("total", 0) or 0
    final = messages[-1] if messages else {}
    final_text = "".join(c.get("text", "") for c in final.get("content", []) if c.get("type") == "text")
    stop_reason = final.get("stopReason")
    result = {"status": "OK" if process.returncode == 0 and stop_reason == "stop" else "PROCESS_ERROR",
              "exit_code": process.returncode, "elapsed_s": elapsed, "provider": provider,
              "model": model, "thinking": thinking, "usage": usage,
              "tool_calls": sum(e.get("type") == "tool_execution_start" for e in events),
              "stop_reason": stop_reason, "provider_error": final.get("errorMessage"),
              "raw_final_text": final_text}
    try:
        result["answer"] = parse_answer(final_text)
    except (ValueError, json.JSONDecodeError) as exc:
        result["parse_error"] = str(exc)
        if result["status"] == "OK":
            result["status"] = "INVALID_JSON"
    return result


def choose_cases(args: argparse.Namespace, manifest: dict) -> list[dict]:
    entries = manifest["cases"]
    by_id = {e["case_id"]: e for e in entries}
    if args.case:
        unknown = [cid for cid in args.case if cid not in by_id]
        if unknown:
            raise ValueError(f"unknown case ID(s): {', '.join(unknown)}")
        selected = [by_id[cid] for cid in dict.fromkeys(args.case)]
    elif args.segment:
        known = set(manifest["segments"])
        unknown = set(args.segment) - known
        if unknown:
            raise ValueError(f"unknown segment(s): {', '.join(sorted(unknown))}")
        selected = [e for e in entries if e["segment"] in args.segment]
    elif args.all:
        selected = entries
    else:
        selected = [by_id[cid] for cid in DEFAULT_CASES]
    return selected[:args.limit] if args.limit else selected


def config_hash(args: argparse.Namespace) -> str:
    parts = {"provider": args.provider, "model": args.model, "thinking": args.thinking,
             "timeout": args.timeout, "pi": args.pi, "reference": not args.skip_reference,
             "manifest": digest(CORPUS / "manifest.json"),
             "policy": digest(ROOT / "config" / "policy.yaml"),
             "runner": digest(pathlib.Path(__file__)),
             "exporter": digest(ROOT / "evals" / "export_blind_case.py"),
             "instructions": {name: digest(ROOT / "pi_cdd" / name)
                              for name in ("corpus_AGENTS.md", "corpus_task.md",
                                           "corpus_result.schema.json")}}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def summarize(rows: list[dict], *, pi_requested: bool, reference_requested: bool) -> dict:
    summary = {"cases": len(rows), "by_segment": {}, "by_risk_band": {}}
    for dimension, name in (("segment", "by_segment"), ("risk_band", "by_risk_band")):
        for value in sorted({row[dimension] for row in rows}):
            subset = [row for row in rows if row[dimension] == value]
            summary[name][value] = {"cases": len(subset)}
            for engine in ("reference", "pi"):
                if (engine == "pi" and not pi_requested) or (engine == "reference" and not reference_requested):
                    continue
                runs = [row.get(engine) or {} for row in subset]
                scores = [run.get("score") for run in runs if run.get("score")]
                summary[name][value][engine] = {
                    "status_counts": dict(Counter(run.get("status", "NOT_RUN") for run in runs)),
                    "answer_count": len(scores),
                    "answer_rate_pct": round(100 * len(scores) / len(subset), 2),
                    "field_accuracy_pct": round(100 * sum(s["fields_correct"] for s in scores) /
                                                (len(subset) * scores[0]["fields_total"]), 2)
                    if scores else 0.0,
                    "elapsed_s_total": round(sum(run.get("elapsed_s", 0) for run in runs), 3),
                }
    for engine in ("reference", "pi"):
        if (engine == "pi" and not pi_requested) or (engine == "reference" and not reference_requested):
            continue
        runs = [row.get(engine) or {} for row in rows]
        scores = [run.get("score") for run in runs if run.get("score")]
        total_fields = len(gold_expectations(read(CORPUS / rows[0]["case_id"] / "gold.json"))) if rows else 0
        summary[engine] = {
            "status_counts": dict(Counter(run.get("status", "NOT_RUN") for run in runs)),
            "answers": len(scores),
            "answer_rate_pct": round(100 * len(scores) / len(rows), 2) if rows else 0.0,
            "all_case_field_accuracy_pct": round(100 * sum(s["fields_correct"] for s in scores) /
                                                 (len(rows) * total_fields), 2) if rows else 0.0,
            "answered_case_field_accuracy_pct": round(100 * sum(s["fields_correct"] for s in scores) /
                                                      (len(scores) * total_fields), 2) if scores else None,
            "risk_band_exact": sum(s["checks"]["risk_band"] for s in scores),
            "risk_band_within_one": sum(s["risk_within_one_band"] for s in scores),
            "recommendation_exact": sum(s["checks"]["recommendation"] for s in scores),
            "recommendation_policy_admissible": sum(s["recommendation_policy_admissible"] for s in scores),
            "required_gates_exact": sum(s["checks"]["required_gates"] for s in scores),
            "elapsed_s_total": round(sum(run.get("elapsed_s", 0) for run in runs), 3),
            "quotes_exact": sum(s["quotes_exact"] for s in scores),
            "claims": sum(s["claims"] for s in scores),
        }
        if engine == "pi":
            summary[engine]["schema_valid_answers"] = sum(s["schema_valid"] for s in scores)
            summary[engine]["provider_cost_usd_total"] = round(
                sum((run.get("usage") or {}).get("cost_usd", 0) for run in runs), 6)
            summary[engine]["tokens_input_total"] = sum(
                (run.get("usage") or {}).get("input", 0) for run in runs)
            summary[engine]["tokens_output_total"] = sum(
                (run.get("usage") or {}).get("output", 0) for run in runs)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", help="case ID; repeat to compare multiple cases")
    parser.add_argument("--segment", action="append", help="select all cases in a segment")
    parser.add_argument("--all", action="store_true", help="select all 260 cases")
    parser.add_argument("--limit", type=int, help="take the first N selected cases")
    parser.add_argument("--pi", action="store_true", help="also run the live Pi agent")
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    parser.add_argument("--thinking", default="off")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per Pi case")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--resume", action="store_true", help="reuse completed matching case results")
    parser.add_argument("--dry-run", action="store_true", help="print selected cases without running")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.skip_reference and not args.pi:
        parser.error("nothing to run: --skip-reference requires --pi")
    manifest = read(CORPUS / "manifest.json")
    entries = choose_cases(args, manifest)
    if args.dry_run:
        print(json.dumps({"cases": [e["case_id"] for e in entries], "count": len(entries),
                          "pi": args.pi, "reference": not args.skip_reference}, indent=2))
        return 0
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    run_hash = config_hash(args)
    rows = []
    for position, entry in enumerate(entries, 1):
        cid = entry["case_id"]
        case_out = out / cid
        case_out.mkdir(parents=True, exist_ok=True)
        row_path = case_out / "result.json"
        if args.resume and row_path.exists():
            prior = read(row_path)
            if prior.get("config_hash") == run_hash and prior.get("gold_sha256") == entry["gold_sha256"]:
                rows.append(prior)
                print(f"[{position}/{len(entries)}] {cid}: reused")
                continue
        with tempfile.TemporaryDirectory(prefix=f"cdd-{cid.lower()}-") as tmp:
            workspace = pathlib.Path(tmp) / "case"
            export(cid, workspace)
            connector = connector_page(workspace, cid)
            pages = page_lookup(workspace, connector)
            row = {"case_id": cid, "segment": entry["segment"], "risk_band": entry["risk_band"],
                   "gold_sha256": entry["gold_sha256"], "config_hash": run_hash,
                   "evaluated_at_utc": datetime.now(timezone.utc).isoformat()}
            if not args.skip_reference:
                row["reference"] = reference_run(workspace, case_out, pages)
            if args.pi:
                row["pi"] = pi_run(workspace, case_out, cid, provider=args.provider,
                                   model=args.model, thinking=args.thinking, timeout=args.timeout)
            gold = read(CORPUS / cid / "gold.json")
            for engine in ("reference", "pi"):
                run = row.get(engine)
                if run and run.get("status") in {"OK", "COMPLETE", "PARTIAL", "AWAITING_HUMAN"} and isinstance(run.get("answer"), dict):
                    run["score"] = score_answer(run["answer"], gold, pages,
                                                require_pi_schema=(engine == "pi"))
            row_path.write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n")
            rows.append(row)
            summary = ", ".join(f"{name}={row[name]['status']}"
                                for name in ("reference", "pi") if name in row)
            print(f"[{position}/{len(entries)}] {cid}: {summary}")
    report = {"scope": "synthetic oracle; no human-reviewed release gate",
              "reference_note": "reference media lookup is input-driven, but other demo-specific output is rejected before scoring",
              "config_hash": run_hash, "selected_case_ids": [e["case_id"] for e in entries],
              "summary": summarize(rows, pi_requested=args.pi, reference_requested=not args.skip_reference),
              "cases": rows}
    (out / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
