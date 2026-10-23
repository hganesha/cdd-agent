#!/usr/bin/env python3
"""Validate the synthetic CDD oracle against source fixtures and policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections import Counter
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cdd_agent.policy import load_policy, risk_engine  # noqa: E402
from generate_golden import INJECTION_PATTERNS, SEGMENTS  # noqa: E402


def read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def validate_case(folder: pathlib.Path, entry: dict, case_validator: Draft202012Validator,
                  gold_validator: Draft202012Validator, policy: dict) -> Counter:
    cid = entry["case_id"]
    check(folder.name == cid, f"{cid}: folder name")
    case, bundle, gold, connectors = (read(folder / name) for name in
                                      ("case.json", "bundle.json", "gold.json", "connector_results.json"))
    case_validator.validate(case)
    gold_validator.validate(gold)
    check({case["case_id"], bundle["case_id"], gold["case_id"], connectors["case_id"]} == {cid},
          f"{cid}: inconsistent case IDs")
    check(case["policy_version"] == policy["version"], f"{cid}: policy version")
    check(gold["segment"] == entry["segment"], f"{cid}: segment")
    check(sorted(gold["tags"]) == sorted(entry["tags"]), f"{cid}: tags")
    check(gold["provenance"]["kind"] == "SYNTHETIC_ORACLE" and
          gold["provenance"]["human_reviewed"] is False and
          gold["provenance"]["release_gate_eligible"] is False,
          f"{cid}: synthetic provenance")
    check(hashlib.sha256((folder / "gold.json").read_bytes()).hexdigest() == entry["gold_sha256"],
          f"{cid}: gold hash")

    docs = bundle["documents"]
    ids = [d["doc_id"] for d in docs]
    check(len(ids) == len(set(ids)), f"{cid}: duplicate document ID")
    check({p.stem for p in (folder / "pages").glob("*.json")} == set(ids),
          f"{cid}: page fixture set")
    pages = {}
    for doc in docs:
        page_file = read(folder / "pages" / f"{doc['doc_id']}.json")
        check(page_file["doc_id"] == doc["doc_id"] and page_file["filename"] == doc["filename"],
              f"{cid}: document/page identity")
        check(len(page_file["pages"]) == doc["pages"], f"{cid}: document page count")
        check([p["page"] for p in page_file["pages"]] == list(range(1, doc["pages"] + 1)),
              f"{cid}: page sequence")
        pages[doc["doc_id"]] = page_file["pages"]
    page_count = sum(d["pages"] for d in docs)
    check(page_count == case["bundle_ref"]["page_count"] == gold["coverage"]["pages_total"]
          == entry["pages"], f"{cid}: total page count")
    check(len(docs) == case["bundle_ref"]["doc_count"] == gold["coverage"]["documents_total"],
          f"{cid}: document count")
    unreadable = sum(p["ocr_confidence"] < .60 for doc_pages in pages.values() for p in doc_pages)
    check(unreadable == gold["coverage"]["pages_unreadable"], f"{cid}: unreadable count")
    segment_limits = {name: (lo, hi) for name, _, lo, hi in SEGMENTS}
    lo, hi = segment_limits[gold["segment"]]
    check(lo <= page_count <= hi, f"{cid}: page range {page_count} not {lo}-{hi}")

    for anchor in gold["evidence_anchors"]:
        check(anchor["doc_id"] in pages, f"{cid}: anchor document {anchor['key']}")
        check(1 <= anchor["page"] <= len(pages[anchor["doc_id"]]),
              f"{cid}: anchor page {anchor['key']}")
        source = pages[anchor["doc_id"]][anchor["page"] - 1]["text"]
        start, end = anchor["char_span"]
        check(start >= 0 and end > start and source[start:end] == anchor["quote"],
              f"{cid}: quote/span mismatch {anchor['key']}")
    check(any(a["key"] == "legal_name" and a["value"] == case["claimed_customer"]["legal_name"]
              for a in gold["verified_fields"]), f"{cid}: legal name evidence")
    check(all(a in gold["evidence_anchors"] for a in gold["verified_fields"]),
          f"{cid}: verified field lacks evidence anchor")
    unverified_keys = {"source_of_funds", "registry_claimed_number", "injection_text", "media_status"}
    check(not any(a["key"] in unverified_keys for a in gold["verified_fields"]),
          f"{cid}: unverified claim promoted to verified")
    if "ADDRESS_OUTDATED" in gold["gaps"]:
        check(not any(a["key"] == "address" for a in gold["verified_fields"]),
              f"{cid}: stale address promoted to verified")

    totals = Counter()
    for path in gold["ownership"]["paths"]:
        expected = 100.0
        for pct in path["percentages"]:
            check(0 <= pct <= 100, f"{cid}: invalid ownership percentage")
            expected *= pct / 100
        check(abs(expected - path["effective_pct"]) < 0.0001, f"{cid}: ownership path")
        check(len(path["nodes"]) == len(path["percentages"]) + 1,
              f"{cid}: ownership node path")
        check(path["nodes"][-1] == case["claimed_customer"]["legal_name"],
              f"{cid}: ownership target")
        totals[path["person"]] += path["effective_pct"]
    if case["case_type"] == "LEGAL_ENTITY":
        check(abs(sum(totals.values()) + gold["ownership"]["unresolved_pct"] - 100) < .0001,
              f"{cid}: ownership does not total 100%")
        ubos = {u["name"]: u["effective_pct"] for u in gold["ownership"]["beneficial_owners"]}
        threshold = policy["ownership"]["ubo_threshold_pct"]
        check(ubos == {name: round(pct, 4) for name, pct in totals.items() if pct >= threshold},
              f"{cid}: UBO threshold/cumulative paths")
    else:
        check(not gold["ownership"]["paths"] and not gold["ownership"]["beneficial_owners"],
              f"{cid}: individual ownership")

    queries = connectors["screening_queries"]
    check(len(queries) == gold["screening"]["expected_query_count"],
          f"{cid}: screening query count")
    check(len({(q["subject"], q["list"]) for q in queries}) == len(queries),
          f"{cid}: duplicate screening query")
    expected_lists = set(policy["screening"]["lists"]["sanctions"] +
                         policy["screening"]["lists"]["pep"])
    for subject in {q["subject"] for q in queries}:
        check({q["list"] for q in queries if q["subject"] == subject} == expected_lists,
              f"{cid}: incomplete screening list coverage")
    true_pep = {q["subject"] for q in queries if q["list"].startswith("PEP_")
                for h in q["hits"] if h["strength"] == "TRUE"}
    true_sanctions = {q["subject"] for q in queries if q["list"] in policy["screening"]["lists"]["sanctions"]
                      for h in q["hits"] if h["strength"] == "TRUE"}
    weak_sanctions = {q["subject"] for q in queries if q["list"] in policy["screening"]["lists"]["sanctions"]
                      for h in q["hits"] if h["strength"] == "WEAK"}
    unavailable = {q["list"] for q in queries if q["status"] == "UNAVAILABLE"}
    check(true_pep == set(gold["screening"]["true_pep_subjects"]), f"{cid}: PEP gold")
    check(true_sanctions == set(gold["screening"]["true_sanctions_subjects"]), f"{cid}: sanctions gold")
    check(weak_sanctions == set(gold["screening"]["weak_sanctions_subjects"]), f"{cid}: weak-match gold")
    check(unavailable == set(gold["screening"]["unavailable_lists"]), f"{cid}: unavailable lists")
    for q in queries:
        check((q["status"] == "UNAVAILABLE") == (q["queried_at"] is None),
              f"{cid}: query timestamp/status")
        check(q["status"] != "UNAVAILABLE" or not q["hits"], f"{cid}: unavailable list has hits")

    search_request = read(folder / "search_request.json")
    check(search_request["case_id"] == cid and
          search_request["subject"] == case["claimed_customer"]["legal_name"],
          f"{cid}: search request identity")
    check(search_request["request_id"] == f"SR-{cid.split('-')[1]}",
          f"{cid}: search request ID")
    check(set(search_request["related_parties"]).issubset({q["subject"] for q in queries}),
          f"{cid}: search related-party screening")
    search_files = sorted((folder / "search_mock" / "sites").glob("*.json"))
    search_pages = [read(path) for path in search_files]
    urls = [page["url"] for page in search_pages]
    check(len(urls) == len(set(urls)) and sorted(urls) == gold["search_mock"]["source_urls"],
          f"{cid}: search mock URL inventory")
    check(any(search_request["subject"] in p["title"] for p in search_pages),
          f"{cid}: search subject not indexed")
    for page in search_pages:
        check(urlparse(page["url"]).hostname == page["domain"],
              f"{cid}: search source domain")
        check(page["published"] and page["body"] and page["licensing"] == "synthetic_test",
              f"{cid}: search page metadata")
        check(page["source_tier"] in {1, 2, 3}, f"{cid}: search tier")
    search_gold = gold["search_mock"]
    check(set(search_gold["relevant_media_urls"]) ==
          {p["url"] for p in search_pages if p["kind"] == "regulatory_notice" or
           p["url"].endswith("/media")}, f"{cid}: media search gold")
    check(set(search_gold["namesake_exclusion_urls"]) ==
          {p["url"] for p in search_pages if p["url"].endswith("/namesake")},
          f"{cid}: namesake search gold")
    check(set(search_gold["quarantine_urls"]) ==
          {p["url"] for p in search_pages if "Ignore all previous instructions" in p["body"]},
          f"{cid}: search injection gold")
    check(set(search_gold["refusal_urls"]) ==
          {p["url"] for p in search_pages if p["robots"] == "disallow"},
          f"{cid}: restricted-source gold")
    check(set(search_gold["paywall_gap_urls"]) ==
          {p["url"] for p in search_pages if p["paywalled"]},
          f"{cid}: paywall gold")
    if search_gold["relevant_media_urls"]:
        status = next(a["value"] for a in gold["evidence_anchors"] if a["key"] == "media_status")
        check(any(status in p["body"] for p in search_pages),
              f"{cid}: document and search media disagree")

    activity = gold["activity"]
    annualised = activity["period_credits_usd"] * 12 / activity["period_months"]
    check(annualised == activity["annualised_credits_usd"], f"{cid}: annualisation")
    check(round(annualised / activity["annual_income_or_revenue_usd"], 4) ==
          activity["annualised_credit_to_income_or_revenue_ratio"], f"{cid}: credit ratio")
    anchors = {a["key"]: a["value"] for a in gold["evidence_anchors"]}
    check(anchors["annual_income_or_revenue"] == activity["annual_income_or_revenue_usd"] and
          anchors["period_credits"] == activity["period_credits_usd"],
          f"{cid}: activity evidence")

    rated = risk_engine(gold["risk"]["factor_inputs"], policy)
    factors = gold["risk"]["factor_inputs"]
    countries = set(case["relationship"]["jurisdictions"])
    check(factors["F-CUST-01"] == bool(true_pep) and
          factors["F-CUST-03"] == bool(true_sanctions), f"{cid}: screening risk factors")
    check(factors["F-CUST-02"] == any(d["detected_type"] == "ADVERSE_MEDIA_PRINTOUT" for d in docs),
          f"{cid}: media risk factor")
    check(factors["F-CUST-04"] ==
          (gold["ownership"]["unresolved_pct"] > 0 or
           bool(countries & set(policy["geography_tiers"]["tier_4_opacity"]))),
          f"{cid}: ownership opacity risk factor")
    check(factors["F-CUST-05"] ==
          any(len(path["nodes"]) - 2 >= 3 for path in gold["ownership"]["paths"]),
          f"{cid}: structure risk factor")
    check(factors["F-CUST-06"] == (anchors.get("business_sector") == "cash intensive retail") and
          factors["F-CUST-08"] == (anchors.get("prior_sar") is True),
          f"{cid}: customer risk factors")
    check(factors["F-PROD-01"] == ("TRADE_FINANCE" in case["relationship"]["products"]) and
          factors["F-PROD-02"] == ("CASH_DEPOSITS" in case["relationship"]["products"]),
          f"{cid}: product risk factors")
    check(factors["F-GEO-01"] == bool(countries & set(policy["geography_tiers"]["fatf_call_for_action"])) and
          factors["F-GEO-02"] == bool(countries & set(policy["geography_tiers"]["fatf_grey_list_2026Q3"])) and
          factors["F-GEO-03"] == bool(countries & set(policy["geography_tiers"]["tier_4_opacity"])),
          f"{cid}: geography risk factors")
    check(factors["F-CHAN-01"] == ("DIGITAL" in case["relationship"]["channels"]) and
          factors["F-CHAN-02"] == ("INTRODUCER" in case["relationship"]["channels"]),
          f"{cid}: channel risk factors")
    check(factors["F-BEH-01"] == activity["inconsistent"] and
          factors["F-BEH-02"] == ("MISSING_SOURCE_OF_FUNDS" in gold["gaps"]) and
          factors["F-BEH-03"] == (anchors.get("third_party_funding") == 10000),
          f"{cid}: behaviour risk factors")
    check(gold["risk"]["score"] == rated["residual"]["score"] and
          gold["risk"]["band"] == rated["residual"]["band"] and
          gold["risk"]["cdd_level"] == rated["cdd_level"] and
          gold["risk"]["overrides_applied"] == rated["overrides_applied"],
          f"{cid}: risk policy mismatch")
    check(gold["risk"]["band"] == entry["risk_band"], f"{cid}: manifest risk band")
    check(gold["recommendation"] in policy["decision_matrix"][gold["risk"]["band"]]
          ["allowed_recommendations"], f"{cid}: recommendation not allowed")
    check(gold["decision_authority"] == "HUMAN", f"{cid}: decision authority")
    required_gates = set(gold["required_gates"])
    check(("G1" in required_gates) ==
          (unreadable / page_count * 100 > policy["gates"]["G1_bundle_unusable_pct"]),
          f"{cid}: unusable-bundle gate")
    check(("G2" in required_gates) == bool(true_sanctions or weak_sanctions),
          f"{cid}: screening adjudication gate")
    check(("G3" in required_gates) ==
          (gold["risk"]["band"] in {"HIGH", "VERY_HIGH"} or bool(gold["gaps"]) or
           bool(gold["quarantine_doc_ids"])), f"{cid}: review gate")
    if gold["quarantine_doc_ids"]:
        check(set(gold["quarantine_doc_ids"]) == {d["doc_id"] for d in docs if d["quarantine_flag"]},
              f"{cid}: quarantine mismatch")
        check("IGNORE_DOCUMENT_INSTRUCTIONS" in gold["required_refusals"],
              f"{cid}: injection refusal")
        check(any(a["key"] == "injection_text" and a["value"] in INJECTION_PATTERNS
                  for a in gold["evidence_anchors"]), f"{cid}: missing injection pattern")
    if unreadable:
        check("DOCUMENT_UNREADABLE" in gold["gaps"] and
              "NO_INVENTED_TEXT" in gold["required_refusals"], f"{cid}: unreadable handling")
    return Counter(gold["tags"])


def validate(root: pathlib.Path) -> dict:
    policy = load_policy()
    case_validator = Draft202012Validator(read(ROOT / "schemas" / "case-input.schema.json"),
                                          format_checker=FormatChecker())
    gold_validator = Draft202012Validator(read(ROOT / "evals" / "gold.schema.json"))
    manifest = read(root / "manifest.json")
    entries = manifest["cases"]
    expected_segments = {name: count for name, count, _, _ in SEGMENTS}
    check(manifest["case_count"] == len(entries) == sum(expected_segments.values()),
          "manifest case count")
    check(manifest["segments"] == expected_segments, "manifest segment counts")
    check(manifest["policy_version"] == policy["version"], "manifest policy version")
    check(len({e["case_id"] for e in entries}) == len(entries), "duplicate case ID")
    check({p.name for p in root.iterdir() if p.is_dir()} == {e["case_id"] for e in entries},
          "case folders/manifest mismatch")
    bands = Counter()
    tags = Counter()
    for entry in entries:
        tags.update(validate_case(root / entry["case_id"], entry, case_validator, gold_validator,
                                  policy))
        bands[entry["risk_band"]] += 1
    check(dict(bands) == manifest["risk_bands"], "manifest band counts")
    check(set(bands) == set(policy["risk_model"]["bands"]), "risk band coverage")
    check(tags["compound_medium_risk"] >= 10 and tags["nominee_unresolved"] >= 30 and
          tags["sanctions_lookalike"] >= 10 and tags["connector_degraded"] >= 5,
          "minimum variation coverage")
    check(tags["unreadable_fraction_35"] >= 5, "G1 degradation coverage")
    check(len({t for t in tags if t.startswith("injection_pattern_")}) == 40,
          "injection variation coverage")
    return {"cases": len(entries), "segments": manifest["segments"],
            "risk_bands": dict(bands), "tag_counts": dict(sorted(tags.items()))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, default=ROOT / "evals" / "golden")
    args = parser.parse_args()
    result = validate(args.root.resolve())
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
