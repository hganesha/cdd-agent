#!/usr/bin/env python3
"""Reproducible one-case comparison of the reference pipeline and a Pi agent.

Run: .venv/bin/python evals/compare.py [--pi]
The reference's USD/tokens are simulated estimates; Pi usage is provider-reported.
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
import time
from datetime import datetime, timezone

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASE = ROOT / "data" / "case_4831"
DEFAULT_OUT = ROOT / "out" / "benchmark"

import sys
sys.path.insert(0, str(ROOT / "src"))
from cdd_agent.orchestrator import Orchestrator, quick_verify  # noqa: E402
from cdd_agent.skills import SEEDED_LISTS, UNAVAILABLE_LISTS  # noqa: E402
from cdd_agent.policy import load_policy  # noqa: E402


def baseline(out: pathlib.Path) -> dict:
    run_dir = out / "reference"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    start = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        manifest = Orchestrator(CASE, run_dir).run()
    elapsed = time.perf_counter() - start
    verify = quick_verify(run_dir)
    report = json.loads((run_dir / "report.json").read_text())
    pages = load_pages()
    extractions = [json.loads(line) for line in (run_dir / "evidence_ledger.jsonl").read_text().splitlines()
                   if json.loads(line).get("record_type") == "EXTRACTION"]
    quoted = []
    spans = []
    for record in extractions:
        locator = record.get("locator") or {}
        source = pages.get((locator.get("doc_id"), locator.get("page")), "")
        quote = record.get("verbatim_quote") or ""
        quoted.append(bool(quote) and quote in source)
        span = locator.get("char_span")
        spans.append(isinstance(span, list) and len(span) == 2 and
                     all(isinstance(x, int) for x in span) and 0 <= span[0] <= span[1] <= len(source) and
                     source[span[0]:span[1]] == quote)
    return {
        "elapsed_s": round(elapsed, 3),
        "manifest": manifest,
        "chain_valid": verify["chain_valid"],
        "duplicate_evidence": verify["duplicate_content_records"],
        "extraction_quotes_exact": sum(quoted),
        "extraction_records": len(extractions),
        "extraction_spans_exact": sum(spans),
        "report_path": str(run_dir / "report.json"),
        "report_keys": sorted(report),
        "cost_kind": "simulated estimate",
    }


def prepare_pi_workspace(out: pathlib.Path) -> pathlib.Path:
    workspace = out / "pi_workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / "data").mkdir(parents=True)
    (workspace / "config").mkdir()
    (workspace / "connectors").mkdir()
    shutil.copytree(CASE, workspace / "data" / "case_4831")
    shutil.copy2(ROOT / "config" / "policy.yaml", workspace / "config" / "policy.yaml")
    shutil.copy2(ROOT / "pi_cdd" / "AGENTS.md", workspace / "AGENTS.md")
    lines = ["SIMULATED SCREENING CONNECTOR RESPONSES — CASE-4831",
             "These are seeded test responses, not live screening or clearance."]
    for list_name, hits in sorted(SEEDED_LISTS.items()):
        if hits:
            for hit in hits:
                lines.append(f"{list_name} HIT: name={hit['name']}; DOB={hit['dob']}; "
                             f"country={hit['country']}; score={hit['score']}; "
                             f"case_ref={hit['case_ref']}; role={hit.get('role', 'n/a')}")
        else:
            lines.append(f"{list_name}: zero hits in seeded response")
    lines.append(f"UNAVAILABLE_LISTS: {', '.join(sorted(UNAVAILABLE_LISTS))}; no clearance may be asserted")
    connector = {"doc_id": "CONNECTOR-4831-001", "pages": [{"page": 1, "text": "\n".join(lines)}]}
    (workspace / "connectors" / "screening.json").write_text(json.dumps(connector, indent=2) + "\n")
    return workspace


def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    return json.loads(text)


def pi_run(out: pathlib.Path, model: str, timeout: int, thinking: str, *, prompt: str | None = None,
           prepare: bool = True, suffix: str = "") -> dict:
    workspace = prepare_pi_workspace(out) if prepare else out / "pi_workspace"
    command = ["pi", "--mode", "json", "--print", "--no-session", "--no-extensions",
               "--no-skills", "--provider", "openrouter", "--model", model,
               "--thinking", thinking, "--tools", "read,grep,find,ls,bash",
               "@data/case_4831/case.json", "@data/case_4831/bundle.json",
               "@config/policy.yaml", "@connectors/screening.json",
               prompt or (ROOT / "pi_cdd" / "task.md").read_text()]
    start = time.perf_counter()
    try:
        process = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                                 timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "TIMEOUT", "elapsed_s": round(time.perf_counter() - start, 3),
                "error": str(exc), "model": model}
    elapsed = time.perf_counter() - start
    (out / f"pi_events{suffix}.jsonl").write_text(process.stdout)
    (out / f"pi_stderr{suffix}.txt").write_text(process.stderr)
    events = []
    for line in process.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    messages = [e["message"] for e in events if e.get("type") == "message_end"
                and e.get("message", {}).get("role") == "assistant"]
    usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost_usd": 0.0}
    for message in messages:
        u = message.get("usage") or {}
        for key in ("input", "output", "cacheRead", "cacheWrite"):
            usage[key] += u.get(key, 0) or 0
        usage["cost_usd"] += (u.get("cost") or {}).get("total", 0) or 0
    final = messages[-1] if messages else {}
    final_text = "".join(c.get("text", "") for c in final.get("content", [])
                         if c.get("type") == "text")
    stop_reason = final.get("stopReason")
    result = {"status": "OK" if process.returncode == 0 and stop_reason == "stop" else "PROCESS_ERROR",
              "exit_code": process.returncode, "elapsed_s": round(elapsed, 3),
              "model": model, "thinking": thinking,
              "pi_version": subprocess.check_output(["pi", "--version"], text=True).strip(),
              "usage": usage, "tool_calls": sum(e.get("type") == "tool_execution_start" for e in events),
              "stop_reason": stop_reason, "provider_error": final.get("errorMessage"),
              "raw_final_text": final_text}
    try:
        result["answer"] = extract_json(final_text)
    except ValueError as exc:
        if result["status"] == "OK":
            result["status"] = "INVALID_JSON"
        result["parse_error"] = str(exc)
    return result


def load_pages() -> dict:
    pages = {}
    for path in (CASE / "pages").glob("*.json"):
        doc = json.loads(path.read_text())
        pages.update({(doc["doc_id"], p["page"]): p.get("text", "") for p in doc["pages"]})
    return pages


def score_pi(answer: dict, out: pathlib.Path) -> dict:
    pages = load_pages()
    connector_path = out / "pi_workspace" / "connectors" / "screening.json"
    if connector_path.exists():
        connector = json.loads(connector_path.read_text())
        pages.update({(connector["doc_id"], p["page"]): p["text"] for p in connector["pages"]})
    claims = answer.get("claims", [])
    quote_checks = [isinstance(c, dict) and isinstance(c.get("quote"), str)
                    and bool(c.get("quote")) and c["quote"] in pages.get((c.get("doc_id"), c.get("page")), "")
                    for c in claims]
    valid = sum(quote_checks)
    expected = {
        "risk_band": "HIGH", "cdd_level": "EDD", "recommendation": "HOLD_FOR_RFI",
        "unresolved_ubo": True, "pep_match": True,
        "sanctions_lookalike_not_cleared": True, "adverse_media_allegation": True,
        "activity_inconsistency": True, "injection_quarantined": True,
        "illegible_pages": 3,
    }
    checks = {key: answer.get(key) == value for key, value in expected.items()}
    checks["gates_exact"] = set(answer.get("required_gates", [])) == {"G2", "G3"}
    owners = answer.get("beneficial_owners", [])
    checks["ubo_set_exact"] = len(owners) == 1 and "Vasquez" in owners[0].get("name", "")
    checks["elena_effective_pct"] = any("Vasquez" in b.get("name", "") and
                                         b.get("effective_pct") == 37 for b in owners)
    required_types = {"OWNERSHIP", "PEP", "SANCTIONS", "MEDIA", "ACTIVITY", "INJECTION", "IDENTITY", "GAP"}
    actual_types = {c.get("type") for c in claims if isinstance(c, dict)}
    checks["claim_type_coverage"] = required_types.issubset(actual_types)
    risk_band = answer.get("risk_band")
    matrix = load_policy().get("decision_matrix", {})
    recommendation_admissible = answer.get("recommendation") in matrix.get(risk_band, {}).get("allowed_recommendations", [])
    return {"checks": checks, "seeded_checks_passed": sum(checks.values()),
            "seeded_checks_total": len(checks), "claims": len(claims),
            "quotes_exact": valid, "quote_validity_pct": round(valid / len(claims) * 100, 1) if claims else 0.0,
            "claim_types": sorted(t for t in actual_types if t),
            "missing_claim_types": sorted(required_types - actual_types),
            "invalid_quote_indices": [i for i, ok in enumerate(quote_checks) if not ok],
            "recommendation_policy_admissible": recommendation_admissible}


def validate_pi_answer(run: dict, out: pathlib.Path) -> list[str]:
    if "answer" not in run:
        return ["Return a complete parseable JSON object."]
    answer = run["answer"]
    schema = json.loads((ROOT / "pi_cdd" / "result.schema.json").read_text())
    schema_errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(answer),
                           key=lambda error: str(error.absolute_path))
    if not isinstance(answer, dict):
        issues = ["Output must be a JSON object."]
        run["schema_errors"] = [error.message for error in schema_errors]
        run["delivery_status"] = "BLOCKED_VALIDATION"
        return issues
    try:
        score = score_pi(answer, out)
    except (TypeError, ValueError, KeyError) as exc:
        issues = ["Schema: " + error.message for error in schema_errors]
        issues.append(f"Unable to score malformed output: {exc}")
        run["schema_errors"] = [error.message for error in schema_errors]
        run["delivery_status"] = "BLOCKED_VALIDATION"
        return issues
    run["score"] = score
    policy = load_policy()
    threshold = policy["ownership"]["ubo_threshold_pct"]
    owners = answer.get("beneficial_owners", [])
    band_order = ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
    band = answer.get("risk_band")
    gates = set(answer.get("required_gates", []))
    issues = ["Schema: " + error.message for error in schema_errors]
    run["schema_errors"] = [error.message for error in schema_errors]
    if score["invalid_quote_indices"]:
        issues.append("Quotes at claims indices " + ", ".join(map(str, score["invalid_quote_indices"])) +
                      " are not exact substrings of their stated source page. Copy a short actual line.")
    if score["missing_claim_types"]:
        issues.append("Missing cited claim types: " + ", ".join(score["missing_claim_types"]))
    if not score["recommendation_policy_admissible"]:
        issues.append("Recommendation is not allowed by the policy decision matrix for the chosen band.")
    if not gates <= {"G1", "G2", "G3", "G4"}:
        issues.append("Required gates must be only the exact codes G1, G2, G3, G4.")
    if "G1" in gates and isinstance(answer.get("illegible_pages"), (int, float)) and \
            answer["illegible_pages"] / 300 * 100 <= policy["gates"]["G1_bundle_unusable_pct"]:
        issues.append("G1 is not triggered at this unusable-page percentage.")
    if "G4" in gates and band in {"HIGH", "VERY_HIGH"}:
        issues.append("G4 low-risk sampling is not applicable to a high-risk case.")
    if (answer.get("unresolved_ubo") or answer.get("injection_quarantined") or
            band in {"HIGH", "VERY_HIGH"}) and "G3" not in gates:
        issues.append("Recheck the G3 triggers for unresolved ownership, injection, or high risk.")
    if answer.get("sanctions_lookalike_not_cleared") and "G2" not in gates:
        issues.append("Recheck G2 routing for the recorded sanctions hit under the reference gate policy.")
    for i, owner in enumerate(owners):
        if not isinstance(owner, dict) or not isinstance(owner.get("effective_pct"), (int, float)) or \
                owner["effective_pct"] < threshold or any(
                    term in owner.get("name", "").lower() for term in ("trust", "limited", "nominees")):
            issues.append(f"Beneficial owner entry {i} is not a natural person at or above {threshold}%. "
                          "Aggregate paths for each natural person; report nominee chains separately.")
    if answer.get("pep_match") and (band not in band_order or band_order.index(band) < band_order.index("HIGH")):
        issues.append("The PEP band floor in the policy requires at least HIGH risk.")
    score["structural_policy_valid"] = not issues or all(
        x.startswith("Quotes at claims") or x.startswith("Missing cited claim types") for x in issues)
    run["delivery_status"] = "PASS" if not issues and run["status"] == "OK" else "BLOCKED_VALIDATION"
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pi", action="store_true", help="also run the live Pi agent")
    parser.add_argument("--repair", action="store_true", help="ask Pi once to correct validation failures")
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    parser.add_argument("--thinking", default="off")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = {"case_id": "CASE-4831", "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "single synthetic case; seeded checks are not senior-analyst gold",
              "pi_instruction_hashes": {
                  name: "sha256:" + hashlib.sha256((ROOT / "pi_cdd" / name).read_bytes()).hexdigest()
                  for name in ("AGENTS.md", "task.md", "result.schema.json")},
              "reference": baseline(out)}
    if args.pi:
        result["pi"] = pi_run(out, args.model, args.timeout, args.thinking)
        issues = validate_pi_answer(result["pi"], out)
        if args.repair and issues and result["pi"]["status"] == "OK":
            initial = result["pi"]
            feedback = ((ROOT / "pi_cdd" / "task.md").read_text() + "\n\nYour previous JSON response was:\n" +
                        json.dumps(initial["answer"], indent=2) + "\n\nValidation issues:\n" +
                        "\n".join(f"- {issue}" for issue in issues) +
                        "\nRevise the entire JSON object. Recheck sources. Return JSON only.")
            result["pi_initial"] = initial
            result["pi"] = pi_run(out, args.model, args.timeout, args.thinking, prompt=feedback,
                                  prepare=False, suffix="_repair")
            validate_pi_answer(result["pi"], out)
            result["pi"]["cumulative_elapsed_s"] = round(initial["elapsed_s"] + result["pi"]["elapsed_s"], 3)
            result["pi"]["cumulative_cost_usd"] = round(initial["usage"]["cost_usd"] +
                                                        result["pi"]["usage"]["cost_usd"], 8)
    (out / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"reference": {"elapsed_s": result["reference"]["elapsed_s"],
                                   "risk": result["reference"]["manifest"]["risk"]["residual"]["band"],
                                   "chain_valid": result["reference"]["chain_valid"]},
                      "pi": {k: v for k, v in result.get("pi", {}).items()
                             if k in {"status", "delivery_status", "elapsed_s", "cumulative_elapsed_s",
                                      "cumulative_cost_usd", "usage", "score", "model", "tool_calls"}}}, indent=2))
    return 0 if not args.pi or result["pi"]["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
