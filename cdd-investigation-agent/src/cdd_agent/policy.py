"""Policy pack loader + deterministic risk engine.

The risk engine is a pure function of (factor inputs, policy version). No LLM anywhere near it.
"""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "policy.yaml"

RDE_LIST = [
    ("RDE-01", "Legal name verified", ["CDD", "EDD"]),
    ("RDE-02", "Entity type / date of birth verified", ["CDD", "EDD"]),
    ("RDE-03", "Incorporation / birth date verified", ["CDD", "EDD"]),
    ("RDE-04", "Registered address verified", ["CDD", "EDD"]),
    ("RDE-05", "Identifier verified and unexpired", ["CDD", "EDD"]),
    ("RDE-06", "Nationality / jurisdiction of incorporation", ["CDD", "EDD"]),
    ("RDE-07", "All beneficial owners >= threshold identified", ["CDD", "EDD"]),
    ("RDE-08", "Beneficial owner identities verified", ["CDD", "EDD"]),
    ("RDE-09", "Control person identified (control prong)", ["CDD", "EDD"]),
    ("RDE-10", "Nature and purpose of relationship", ["CDD", "EDD"]),
    ("RDE-11", "Expected activity profile", ["CDD", "EDD"]),
    ("RDE-12", "Source of funds", ["CDD", "EDD"]),
    ("RDE-13", "Source of wealth", ["EDD"]),
    ("RDE-14", "Sanctions screening — all required parties", ["CDD", "EDD"]),
    ("RDE-15", "PEP screening — all required parties", ["CDD", "EDD"]),
    ("RDE-16", "Adverse media within lookback window", ["CDD", "EDD"]),
    ("RDE-17", "Registry corroboration of corporate existence", ["CDD", "EDD"]),
    ("RDE-18", "Document recency within policy window", ["CDD", "EDD"]),
    ("RDE-19", "Risk rating trace recorded", ["CDD", "EDD"]),
    ("RDE-20", "Ownership structure chart validated", ["CDD", "EDD"]),
    ("RDE-21", "Authorised signatories identified and screened", ["CDD", "EDD"]),
    ("RDE-22", "Business nature / occupation corroborated", ["CDD", "EDD"]),
    ("RDE-23", "Tax identifier captured", ["EDD"]),
    ("RDE-24", "EDD approval record (human decision)", ["EDD"]),
]


def load_policy(path: pathlib.Path | None = None) -> dict:
    return yaml.safe_load((path or POLICY_PATH).read_text())


def geography_factor(countries: list[str], policy: dict) -> tuple[str, int, list[str]]:
    """Maps jurisdictions to a single geography factor per the published tier table."""
    tiers = policy["geography_tiers"]
    hits: list[str] = []
    if set(countries) & set(tiers["fatf_call_for_action"]):
        hits.append("F-GEO-01")
    if set(countries) & set(tiers["fatf_grey_list_2026Q3"]):
        hits.append("F-GEO-02")
    if set(countries) & set(tiers["tier_4_opacity"]):
        hits.append("F-GEO-03")
    return (hits[0] if hits else "F-GEO-T1"), 0, hits


def risk_engine(factor_inputs: dict[str, bool], policy: dict) -> dict:
    """Deterministic: same inputs + same policy version => identical output. No randomness, no LLM.

    score  = Σ (dimension_weight × min(100, Σ points triggered in that dimension))
    band   = max(score_band, highest applicable override floor)
    """
    model = policy["risk_model"]
    dims = model["dimensions"]
    factors = {f["id"]: f for f in model["factors"]}
    band_order = ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]

    trace, dim_points = [], {d: 0.0 for d in dims}
    for fid, applies in sorted(factor_inputs.items()):
        if not applies:
            continue
        if fid not in factors:
            raise KeyError(f"factor {fid} is not in the permitted vocabulary (policy {policy['version']})")
        f = factors[fid]
        dim_points[f["dimension"]] += f["points"]
        trace.append({"factor": f["name"], "factor_id": fid, "policy_rule_id": fid,
                      "dimension": f["dimension"], "weight": dims[f["dimension"]]["weight"],
                      "input": "APPLIES", "raw_points": f["points"], "contribution": 0.0})

    inherent = 0.0
    for d, w in dims.items():
        dim_score = min(100.0, dim_points[d])
        inherent += dim_score * w["weight"]
        for t in trace:
            if t["dimension"] == d:
                t["contribution"] = round(t["raw_points"] * w["weight"], 2)
    inherent = round(inherent, 2)

    bands = model["bands"]
    def band_for(score: float) -> str:
        for name, rng in bands.items():
            if rng["min"] <= score <= rng["max"]:
                return name
        return "VERY_HIGH"

    score_band = band_for(inherent)
    floor, overrides_applied = "LOW", []
    for ov in model.get("overrides", []):
        if factor_inputs.get(ov["if_factor"]):
            overrides_applied.append({"factor_id": ov["if_factor"], "min_band": ov["min_band"],
                                      "reason": ov["reason"]})
            if band_order.index(ov["min_band"]) > band_order.index(floor):
                floor = ov["min_band"]

    final_band = score_band if band_order.index(score_band) >= band_order.index(floor) else floor
    if overrides_applied:
        trace.append({"factor": "BAND_OVERRIDE", "factor_id": "OVERRIDE", "policy_rule_id": "RISK-OVR-01",
                      "dimension": "customer", "weight": 0.0, "input": "FLOOR_APPLIED",
                      "raw_points": 0, "contribution": 0.0,
                      "note": f"score band {score_band} raised to {final_band} by "
                              f"{', '.join(o['factor_id'] for o in overrides_applied)}"})

    return {
        "engine_version": "risk-engine@1.0.0",
        "policy_version": policy["version"],
        "inherent": {"score": inherent, "band": final_band, "weighted_score_band": score_band},
        "mitigants": [],
        "residual": {"score": inherent, "band": final_band},
        "overrides_applied": overrides_applied,
        "cdd_level": model["cdd_level"][final_band],
        "review_cadence": model["review_cadence"][final_band],
        "trace": trace,
    }


def decision_gate(band: str, coverage_pct: float, findings: list[dict], policy: dict) -> list[str]:
    gates: list[str] = []
    min_cov = policy["decision_matrix"][band]["min_coverage_pct"]
    if coverage_pct < min_cov:
        gates.append("G3")
    force = policy["gates"]["G3_force_on"]
    types = {f["type"] for f in findings if f["status"] == "OPEN"}
    if band in ("HIGH", "VERY_HIGH") and "RESIDUAL_HIGH_OR_ABOVE" in force:
        gates.append("G3")
    for cond, trigger in [("UNRESOLVED_UBO", "UNRESOLVED_UBO"),
                          ("CONTRADICTION", "MATERIAL_CONTRADICTION"),
                          ("INJECTION_ATTEMPT", "INJECTION_ATTEMPT"),
                          ("SCREENING_INCOMPLETE", "SCREENING_INCOMPLETE")]:
        if cond in types:
            gates.append("G3")
    if "SANCTIONS_HIT" in types:
        gates.append("G2")
    return sorted(set(gates))
