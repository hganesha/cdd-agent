"""Executable tests for AXIOM-POLICY.

Run: python3 tests/test_policy_agent.py      (or pytest -q)
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from axiom.author import PolicyAuthor
from axiom.bundle import PolicyBundleService
from axiom.evidence import DecisionLedger, IdempotencyStore, canonical, sha256_str
from axiom.opa import OPAError, OPAClient

OPA = OPAClient()
POLICY = ROOT / "policy"
SCHEMAS = ROOT / "schemas"
OUT = ROOT / "out" / "tests"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def _clean_case(**over) -> dict:
    base = json.loads((ROOT / "corpus" / "decisions" / "clean_individual.json").read_text())
    for k, v in over.items():
        base[k] = v if not isinstance(v, dict) or not isinstance(base.get(k), dict) else {**base[k], **v}
    return base


def _decide(payload: dict, policy_path: pathlib.Path = POLICY) -> dict:
    return OPA.eval("data.agent.decision.result", data_paths=[policy_path], input_obj=payload).value


def _fresh_ledger(name: str) -> DecisionLedger:
    path = OUT / name / "ledger.jsonl"
    if path.parent.exists():
        shutil.rmtree(path.parent)
    return DecisionLedger(path, agent_id="AXIOM-POLICY",
                          idem=IdempotencyStore(path.parent / "idem.json"))


# ── compile / capability confinement ─────────────────────────────────────────
def test_policy_compiles_under_restricted_capabilities():
    ok, msg = OPA.check(POLICY, restricted=True)
    assert ok, msg


def test_policy_cannot_use_network_builtins():
    """A policy that tries to reach the network must fail to compile, not fail in production."""
    tmp = OUT / "capability_probe"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(POLICY, tmp)
    (tmp / "cdd" / "bad.rego").write_text(
        "package cdd.bad\n\nimport rego.v1\n\n"
        "fetch := resp if {\n\tresp := http.send({\"method\": \"get\", \"url\": \"https://example.com\"})\n}\n"
    )
    ok, msg = OPA.check(tmp, restricted=True)
    assert not ok
    assert "http.send" in msg


def test_rego_suite_passes():
    ok, out = OPA.test(POLICY)
    assert ok, out[-2000:]


# ── decision contract ────────────────────────────────────────────────────────
def test_decision_matches_schema():
    d = _decide(_clean_case())
    jsonschema.validate(d, _schema("decision.schema.json"))


def test_outcome_is_a_closed_enum_across_the_corpus():
    allowed = {"PERMIT", "PERMIT_WITH_CONDITIONS", "REQUIRE_RFI", "REQUIRE_EDD", "BLOCK"}
    for f in sorted((ROOT / "corpus" / "decisions").glob("*.json")):
        d = _decide(json.loads(f.read_text()))
        assert d["outcome"] in allowed, f"{f.stem}: {d['outcome']}"


def test_decision_is_deterministic():
    payload = _clean_case()
    a, b = _decide(payload), _decide(payload)
    assert a["decision_id"] == b["decision_id"]
    assert a["outcome"] == b["outcome"] and a["reasons"] == b["reasons"]


def test_sanctions_true_hit_never_permits_and_never_clears():
    for strength in ("TRUE", "STRONG"):
        d = _decide(_clean_case(screening={"hits": [{"kind": "SANCTIONS", "strength": strength,
                                                    "list": "OFAC_SDN"}]}))
        assert d["outcome"] == "BLOCK"
        assert "SANCTIONS_HIT_REQUIRES_ADJUDICATION" in d["reasons"]
        assert d["agent_may_clear_sanctions_hit"] is False
        assert d["gates"]["route_to"] == "MLRO"


def test_weak_lookalike_is_not_escalated():
    d = _decide(_clean_case(screening={"hits": [{"kind": "SANCTIONS", "strength": "WEAK",
                                                "list": "OFAC_SDN"}]}))
    assert d["outcome"] == "PERMIT"
    assert d["screening"]["requires_adjudication"] is False


def test_unknown_factor_blocks_rather_than_being_ignored():
    d = _decide(_clean_case(factors={"F-INVENTED-01": True}))
    assert d["outcome"] == "BLOCK"
    assert "UNKNOWN_RISK_FACTOR" in d["reasons"]


def test_gate_routing_and_obligations():
    d = _decide(_clean_case(factors={"F-CUST-01": True}, risk={"cdd_level": "EDD", "band": "HIGH"}))
    assert d["outcome"] == "REQUIRE_EDD"
    assert d["gates"]["route_to"] == "EDD_UNIT"
    assert {o["kind"] for o in d["obligations"]} >= {"HUMAN_EDD_APPROVAL"}


# ── parity with the CDD agent's independent Python engine ────────────────────
def _load_cdd_engine():
    path = ROOT.parent / "cdd-investigation-agent" / "src" / "cdd_agent" / "policy.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("cdd_policy_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_python_and_opa_risk_engines_agree():
    """Two independent implementations of the same policy must produce the same band.

    This is the check that catches a policy change applied to only one engine — the most common
    way a second implementation of a control silently diverges.
    """
    ref = _load_cdd_engine()
    if ref is None:
        print("      (skipped: cdd-investigation-agent not present)")
        return
    ref_policy = ref.load_policy()
    factors_cfg = ref_policy["risk_model"]["factors"]
    factor_ids = sorted(f["id"] if isinstance(f, dict) else f for f in factors_cfg)
    combos = [
        {},
        {"F-CUST-01": True},
        {"F-CUST-03": True},
        {"F-CUST-04": True},
        {"F-CUST-01": True, "F-CUST-02": True, "F-CUST-04": True, "F-CUST-05": True,
         "F-PROD-01": True, "F-GEO-03": True, "F-BEH-01": True},
        {"F-GEO-01": True},
        {"F-PROD-02": True, "F-CHAN-01": True},
        {fid: True for fid in factor_ids},
    ]
    mismatches = []
    for combo in combos:
        payload = _clean_case(factors=combo)
        opa = _decide(payload)
        py = ref.risk_engine(combo, ref_policy)
        if opa["risk"]["band"] != py["residual"]["band"] or abs(opa["risk"]["score"] - py["residual"]["score"]) > 0.01:
            mismatches.append({"factors": combo, "opa": opa["risk"], "python": py["residual"]})
    assert not mismatches, json.dumps(mismatches, indent=1)[:1200]


# ── bundle lifecycle ────────────────────────────────────────────────────────
def test_bundle_build_verify_and_tamper_detection():
    svc = PolicyBundleService(OPA)
    manifest = svc.build("test-r1")
    jsonschema.validate(manifest, _schema("bundle-manifest.schema.json"))
    assert svc.verify("test-r1")["valid"] is True
    assert manifest["restricted_capabilities"] is True
    assert manifest["tests"]["status"] == "PASS"

    # tamper with a source file: verification must fail and name the file
    target = POLICY / "cdd" / "coverage.rego"
    original = target.read_text()
    try:
        target.write_text(original + "\n# tampered\n")
        v = svc.verify("test-r1")
        assert v["valid"] is False
        assert any("coverage.rego" in p for p in v["problems"])
    finally:
        target.write_text(original)
    assert svc.verify("test-r1")["valid"] is True


def test_activation_requires_a_named_approval():
    svc = PolicyBundleService(OPA)
    svc.build("test-r2")
    with_actor = svc.activate("test-r2", actor="HEAD_OF_FCC", approval_ticket="CAB-118")
    assert with_actor["status"] == "ACTIVE" and with_actor["activated_by"] == "HEAD_OF_FCC"
    assert svc.active()["revision"] == "test-r2"


# ── authoring agent ────────────────────────────────────────────────────────
def test_author_produces_a_draft_that_cannot_be_activated():
    author = PolicyAuthor(OPA)
    change = {"kind": "data_patch", "pointer": "/config/thresholds/ubi_pct", "new": 1}
    change = {"kind": "data_patch", "pointer": "/config/thresholds/rfi_max_rounds",
              "old": 2, "new": 3, "description": "allow a third RFI round"}
    p = author.propose(ROOT / "corpus" / "SOP_CDD_v4.2_excerpt.md",
                       proposal_id="P-TEST-001", change=change)
    jsonschema.validate(p, _schema("policy-proposal.schema.json"))
    assert p["status"] == "DRAFT"
    assert p["activation"]["permitted_by_agent"] is False
    assert p["required_approvals"]
    assert p["validation"]["restricted_capabilities"] is True


def test_author_blocks_a_change_that_breaks_the_test_suite():
    author = PolicyAuthor(OPA)
    change = {"kind": "data_patch", "pointer": "/config/thresholds/ubo_pct", "old": 25, "new": 20,
              "description": "lower UBO threshold (the policy pin test must be updated with it)"}
    p = author.propose(ROOT / "corpus" / "SOP_CDD_v4.2_excerpt.md",
                       proposal_id="P-TEST-002", change=change)
    assert p["validation"]["check_ok"] is True
    assert p["validation"]["tests_ok"] is False, "the threshold pin should catch an unreviewed change"
    assert "skipped" in p["impact"], "impact analysis must not run on a failing candidate"


def test_author_impact_analysis_changes_the_right_cases():
    author = PolicyAuthor(OPA)
    current = json.loads((POLICY / "data.json").read_text())["config"]["screening"]["required_lists"]
    change = {"kind": "data_patch", "pointer": "/config/screening/required_lists",
              "old": current, "new": current + ["LOCAL_REGULATOR"],
              "description": "make the local regulator list mandatory"}
    p = author.propose(ROOT / "corpus" / "SOP_CDD_v4.2_excerpt.md",
                       proposal_id="P-TEST-003", change=change)
    assert p["validation"]["tests_ok"] is True
    impact = p["impact"]
    assert impact["cases_evaluated"] >= 8
    assert impact["cases_changed"] >= 1
    moved = {c["case"] for c in impact["changes"]}
    assert "clean_individual" in moved
    assert all("SCREENING_INCOMPLETE" in c["new_reasons"] for c in impact["changes"]
               if c["case"] == "clean_individual")


def test_author_refuses_a_precondition_mismatch():
    author = PolicyAuthor(OPA)
    change = {"kind": "data_patch", "pointer": "/config/thresholds/ubo_pct", "old": 99, "new": 20}
    p = author.propose(ROOT / "corpus" / "SOP_CDD_v4.2_excerpt.md",
                       proposal_id="P-TEST-004", change=change)
    assert p["validation"]["check_ok"] is False
    assert "precondition failed" in p["validation"]["error"]


# ── authority + evidence ────────────────────────────────────────────────────
def test_authority_denies_prohibited_actions():
    for action in ("case.close", "case.approve", "evidence.delete", "policy.activate",
                   "sanctions.clear", "risk.override"):
        r = OPA.eval("data.agent.authority.result", data_paths=[POLICY], input_obj={"action": action}).value
        jsonschema.validate(r, _schema("authority.schema.json"))
        assert r["decision"] == "denied", action


def test_authority_gates_some_actions_and_allows_the_rest():
    gated = OPA.eval("data.agent.authority.result", data_paths=[POLICY],
                     input_obj={"action": "policy.propose_change"}).value
    assert gated["decision"] == "approval_required"
    assert gated["approval"]["approver"] == "POLICY_OWNER"
    allowed = OPA.eval("data.agent.authority.result", data_paths=[POLICY],
                       input_obj={"action": "policy.evaluate"}).value
    assert allowed["decision"] == "allowed"


def test_evidence_ledger_chains_and_is_idempotent():
    ledger = _fresh_ledger("evidence")
    payload = _clean_case()
    bundle = {"revision": "test-r1", "sha256": "sha256:" + "a" * 64,
              "policy_version": "cdd-policy-2026.4", "opa_version": OPA.version()}
    d = _decide(payload)
    ev1 = ledger.decision(decision=d, input_obj=payload, bundle=bundle, elapsed_ms=12.0)
    ev2 = ledger.decision(decision=d, input_obj=payload, bundle=bundle, elapsed_ms=12.0)
    assert ev1 == ev2, "the same decision must never produce a second evidence record"
    ok, head, broken = ledger.verify_chain()
    assert ok and broken is None
    d2 = _decide(_clean_case(input_hash="other"))
    ledger.decision(decision=d2, input_obj=_clean_case(input_hash="other"), bundle=bundle, elapsed_ms=9.0)
    assert ledger.verify_chain()[0] is True


def test_evidence_ledger_detects_tampering():
    ledger = _fresh_ledger("tamper")
    bundle = {"revision": "test-r1", "sha256": "sha256:" + "a" * 64,
              "policy_version": "cdd-policy-2026.4", "opa_version": OPA.version()}
    ledger.decision(decision=_decide(_clean_case()), input_obj=_clean_case(),
                    bundle=bundle, elapsed_ms=1.0)
    lines = ledger.path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["outcome"] = "PERMIT_WITH_CONDITIONS"  # differs from the recorded PERMIT
    lines[0] = json.dumps(rec)
    ledger.path.write_text("\n".join(lines) + "\n")
    assert DecisionLedger(ledger.path, agent_id="A").verify_chain()[0] is False


def test_decision_id_binds_input_and_policy_version():
    a = _decide(_clean_case())
    b = _decide(_clean_case(input_hash="different"))
    assert a["decision_id"] != b["decision_id"]
    assert a["policy_version"] == b["policy_version"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {str(exc)[:300]}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
