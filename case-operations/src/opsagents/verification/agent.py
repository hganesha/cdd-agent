"""OPS-VERIFY — the independent quality gate.

This agent is the reason the other three can be trusted. It does not gather facts and it does not
decide anything: it re-derives other agents' arithmetic, re-checks their evidence chains, re-plays
the policy decision, and scans their language for the phrases that precede a compliance failure.

Its output has exactly one field the rest of the stack consumes: `critic_status` — `PASS` or `FAIL`.
The decision agent refuses to make a recommendation on a FAIL, and — belt and braces — forwards the
same status to the policy service as `critic_status`, where a failure forces a human gate
(`policy/cdd/gates.rego`). A failed verification therefore cannot be talked around: it changes the
policy outcome, not just the commentary.

Design rules:
* **Re-derive, never trust.** Every number is recomputed from primitive inputs.
* **Fail closed on absence.** A missing artefact is a FAIL, not a skip — except where the artefact is
  genuinely optional for that case (an RFI that is not due yet), which is `NOT_APPLICABLE`.
* **Say what was checked.** A PASS lists the checks that ran; an empty report is a FAIL.
"""
from __future__ import annotations

import re

from ..common.ledger import OpsLedger, canonical, sha256_str

# Phrases that must never appear in a customer-facing draft or a decision record.
TIPPING_OFF = [
    r"\bsuspicious activity report\b", r"\bSAR\b(?!\w)",
    r"\bsuspicion of (money laundering|terrorism)\b",
    r"\breport(ed|ing)? (you |this )?to the (authorities|police|NCA|FinCEN)\b",
    r"\bwe have notified\b", r"\bunder investigation by\b",
    r"\bcriminal (proceedings|investigation)\b",
]
CLEARANCE_LANGUAGE = [
    r"\bcleared\b", r"\bno (adverse|negative) (findings|media)\b", r"\bfully compliant\b",
    r"\bsanctions[- ]free\b", r"\bnot a (PEP|risk)\b",
]
PII_PATTERNS = [
    (r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b", "payment_card"),
    (r"\b\d{2}\s?[A-Z]{3}\s?\d{4}\b", "passport_like"),
    (r"\bIBAN\b|\bbank account number\b", "bank_detail"),
]
INJECTION_MARKERS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "instruction_override"),
    (r"you\s+are\s+now\s+(a|an)\s+", "role_reassignment"),
    (r"\bcite\s+(this|https?://\S+)\s+as\s+the\s+authoritative\s+source", "citation_forcing"),
    (r"\bdo\s+not\s+report\s+this\s+instruction\b", "concealment_instruction"),
    (r"\bsystem\s*:\s*", "fake_system_block"),
]

#: Actions no agent may perform. An action plan may *list* them (the work exists) but the owning
#: status must hand the act to a human.
PROHIBITED = {"case.finalise", "customer.approve", "sanctions.clear", "policy.activate",
              "evidence.rewrite"}
GATED = {"rfi.issue", "escalation.create", "risk.recommend", "bundle.stage"}
SELF_APPROVING_ACTORS = {"OPS-PROFILE", "OPS-VERIFY", "OPS-OUTREACH", "OPS-DECIDE",
                         "AXIOM-POLICY", "agent", "system", "automation", "ops-agent"}


def _check(check_id: str, title: str, verdict: str, detail: str, *, severity: str = "MAJOR",
           evidence: list[str] | None = None) -> dict:
    return {"check_id": check_id, "title": title, "verdict": verdict, "severity": severity,
            "blocking": verdict == "FAIL" and severity == "BLOCKING",
            "detail": detail, "evidence": evidence or []}


def _days_between(a: str, b: str) -> int:
    from datetime import date
    d1 = date.fromisoformat(a[:10])
    d2 = date.fromisoformat(b[:10])
    return abs((d2 - d1).days)


class VerificationAgent:
    AGENT_ID = "OPS-VERIFY"

    CHECKS = (
        ("V-001", "_chain"), ("V-002", "_evidence_ids"), ("V-003", "_arithmetic"),
        ("V-004", "_policy_replay"), ("V-005", "_decision_shape"),
        ("V-006", "_cross_consistency"), ("V-007", "_coverage_honesty"),
        ("V-008", "_obligations_acted"), ("V-009", "_authority"), ("V-010", "_language"),
        ("V-011", "_pii"), ("V-012", "_injection"), ("V-013", "_freshness"),
    )

    def __init__(self, ledger: OpsLedger, policy=None):
        self.ledger = ledger
        self.policy = policy

    # ── public ──────────────────────────────────────────────────────────────
    def verify(self, bundle: dict, *, stage: str = "pre_decision", state=None, kill=None) -> dict:
        """bundle keys (all optional except profile): case, profile, evidence_pack, policy_input,
        policy_decision, disposition, rfi, approval, as_of, actions_used.

        `state` is a `RunState`: pass one and each check is checkpointed, which matters when the
        bundle carries a profile built from hundreds of pages and a re-verification is not free.
        """
        checks: list[dict] = []
        if not bundle.get("profile"):
            checks.append(_check("V-000", "bundle completeness", "FAIL",
                                 "no profile in the bundle: there is nothing to verify",
                                 severity="BLOCKING"))
        for check_id, method in self.CHECKS:
            stage_name = f"check:{check_id}"
            if state is not None and state.done(stage_name):
                checks.append(state.payload(stage_name))
                continue
            if state is not None:
                state.check_budget("checks", what=check_id)
            if kill is not None:
                kill.tick()
            result = getattr(self, method)(bundle)
            checks.append(result)
            if state is not None:
                state.charge(checks=1)
                state.record(stage_name, result, agent=self.AGENT_ID)
        if len([c for c in checks if c["check_id"] != "V-000"]) != len(self.CHECKS):
            checks.append(_check("V-000", "check battery", "FAIL",
                                 f"only {len(checks)}/{len(self.CHECKS)} checks produced a verdict",
                                 severity="BLOCKING"))

        failed = [c for c in checks if c["verdict"] == "FAIL"]
        blocking = [c for c in failed if c["severity"] == "BLOCKING"]
        injection = next((c for c in checks if c["check_id"] == "V-012"), None)
        report = {
            "verification_id": "VER-" + sha256_str(canonical(
                {"stage": stage, "checks": checks}))[7:15],
            "case_id": bundle.get("case", {}).get("case_id"),
            "subject_agent": self._subjects(bundle),
            "stage": stage,
            "critic_status": "FAIL" if (blocking or any(
                c["verdict"] == "FAIL" and c["severity"] == "MAJOR" for c in failed)) else "PASS",
            "injection_attempt": bool(injection and injection["verdict"] == "FAIL"),
            "counts": {"checks": len(checks),
                       "pass": sum(1 for c in checks if c["verdict"] == "PASS"),
                       "fail": len(failed),
                       "not_applicable": sum(1 for c in checks if c["verdict"] == "NOT_APPLICABLE"),
                       "blocking": len(blocking)},
            "checks": checks,
            "blocking_check_ids": [c["check_id"] for c in blocking],
            "agent_id": self.AGENT_ID,
        }
        report["evidence_id"] = self.ledger.verification(report=report)
        return report

    # ── checks ──────────────────────────────────────────────────────────────
    def _chain(self, b: dict) -> dict:
        ok, head, broken = self.ledger.verify_chain()
        if not ok:
            return _check("V-001", "evidence chain integrity", "FAIL",
                          f"ledger chain broken at record {broken}: every artefact that cites it is "
                          f"untrusted", severity="BLOCKING")
        return _check("V-001", "evidence chain integrity", "PASS",
                      f"ops ledger chain valid at {head[:18]}…")

    def _evidence_ids(self, b: dict) -> dict:
        known = {r["evidence_id"] for r in self.ledger.records()}
        profile = b.get("profile") or {}
        cited: list[str] = [profile.get("evidence_id")] if profile.get("evidence_id") else []
        missing: list[str] = []
        for fid, ob in (profile.get("observations") or {}).items():
            ids = ob.get("evidence_ids") or []
            if not ids:
                missing.append(f"{fid} (no evidence)")
            cited.extend(ids)
        for name, artefact in (("rfi", b.get("rfi")), ("disposition", b.get("disposition")),
                               ("verification", b.get("verification"))):
            if artefact and artefact.get("evidence_id"):
                cited.append(artefact["evidence_id"])
        unresolved = sorted({i for i in cited if i and i not in known})
        if unresolved:
            return _check("V-002", "evidence traceability", "FAIL",
                          f"evidence references do not resolve in the ledger: "
                          f"{', '.join(unresolved[:6])}", severity="BLOCKING")
        if missing:
            return _check("V-002", "evidence traceability", "FAIL",
                          f"factors without evidence: {', '.join(missing[:6])}",
                          severity="BLOCKING")
        return _check("V-002", "evidence traceability", "PASS",
                      f"{len(set(cited))} evidence reference(s) resolve; every factor is evidenced")

    def _arithmetic(self, b: dict) -> dict:
        """Recompute the numbers the profile asserts. A tampered or buggy derivation shows up here
        even when every evidence id is valid."""
        profile = b.get("profile")
        if not profile:
            return _check("V-003", "arithmetic re-derivation", "NOT_APPLICABLE", "no profile",
                          severity="MINOR")
        problems = []
        cov = profile["coverage"]
        applicable = [e for e in cov["elements"] if e["status"] != "NOT_APPLICABLE"]
        satisfied = [e for e in applicable if e["status"] == "SATISFIED"]
        pct = round(len(satisfied) / len(applicable) * 100, 1) if applicable else 0.0
        if abs(pct - cov["coverage_pct"]) > 0.05:
            problems.append(f"coverage_pct {cov['coverage_pct']} != recomputed {pct}")

        unresolved = round(sum(u["pct"] for u in profile["ownership"]["unresolved"]), 2)
        if abs(unresolved - profile["ownership"]["unresolved_pct"]) > 0.01:
            problems.append(f"unresolved_pct {profile['ownership']['unresolved_pct']} "
                            f"!= recomputed {unresolved}")

        top = profile["counterparties"]["top"]
        total = sum(c["amount"] for c in top) or 1.0
        hhi = round(sum((c["amount"] / total) ** 2 for c in top), 4)
        if abs(hhi - profile["transactions"]["top_counterparty_hhi"]) > 0.001:
            problems.append(f"hhi {profile['transactions']['top_counterparty_hhi']} "
                            f"!= recomputed {hhi}")

        tx = profile["transactions"]
        if tx["declared_monthly_turnover"]:
            dev = round((tx["observed_monthly_mean"] / tx["declared_monthly_turnover"] - 1) * 100, 1)
            if abs(dev - tx["declared_vs_observed_pct"]) > 0.11:
                problems.append(f"declared_vs_observed {tx['declared_vs_observed_pct']} "
                                f"!= recomputed {dev}")

        triggered = sorted(k for k, v in profile["factors"].items() if v)
        if triggered != sorted(profile["factors_triggered"]):
            problems.append("factors_triggered does not match the factor map")

        if problems:
            return _check("V-003", "arithmetic re-derivation", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        return _check("V-003", "arithmetic re-derivation", "PASS",
                      "coverage, ownership, concentration and turnover deviation all re-derive")

    def _policy_replay(self, b: dict) -> dict:
        decision, pin = b.get("policy_decision"), b.get("policy_input")
        if not decision:
            return _check("V-004", "policy decision replay", "NOT_APPLICABLE",
                          "no policy decision in this bundle", severity="MINOR")
        if not pin:
            return _check("V-004", "policy decision replay", "FAIL",
                          "a policy decision is present but its input was not pinned, so the decision "
                          "cannot be replayed or audited", severity="BLOCKING")
        if self.policy is None:
            return _check("V-004", "policy decision replay", "NOT_APPLICABLE",
                          "policy engine not wired", severity="MINOR")
        replayed = self.policy.decide(pin)
        d1 = {"outcome": decision["outcome"], "reasons": sorted(decision["reasons"]),
              "route": decision["gates"]["route_to"], "band": decision["risk"]["band"],
              "score": decision["risk"]["score"]}
        d2 = {"outcome": replayed["outcome"], "reasons": sorted(replayed["reasons"]),
              "route": replayed["gates"]["route_to"], "band": replayed["risk"]["band"],
              "score": replayed["risk"]["score"]}
        bad = [k for k in ("outcome", "reasons", "route", "band", "score") if d1[k] != d2[k]]
        if bad:
            return _check("V-004", "policy decision replay", "FAIL",
                          f"replaying the decision input from the current policy disagrees on "
                          f"{', '.join(bad)}: recorded {d1}, replayed {d2}", severity="BLOCKING")
        return _check("V-004", "policy decision replay", "PASS",
                      f"recorded {decision['outcome']} reproduces exactly "
                      f"({decision['decision_id'][:32]}…)")

    def _decision_shape(self, b: dict) -> dict:
        decision = b.get("policy_decision")
        if not decision:
            return _check("V-005", "decision envelope integrity", "NOT_APPLICABLE",
                          "no policy decision in this bundle", severity="MINOR")
        problems = []
        if decision["outcome"] not in ("PERMIT", "PERMIT_WITH_CONDITIONS", "REQUIRE_EDD",
                                       "REQUIRE_RFI", "BLOCK"):
            problems.append(f"outcome {decision['outcome']!r} is outside the closed enum")
        if decision.get("agent_may_clear_sanctions_hit") is not False:
            problems.append("the envelope permits an agent to clear a sanctions hit")
        # Auto-finalisation is a platform prohibition, not a policy one: the pack does not publish
        # it, so it is checked where it is projected — on the disposition. Either place is fine,
        # neither is optional.
        auto = decision.get("auto_final_allowed")
        if auto is None:
            auto = ((b.get("disposition") or {}).get("policy") or {}).get("auto_final_allowed")
        if auto is not False:
            problems.append("nothing in the bundle forbids automatic finalisation")
        for key in ("decision_id", "policy_version", "reasons", "gates", "risk", "obligations"):
            if key not in decision:
                problems.append(f"envelope is missing {key}")
        # A bare PERMIT must be unexplained (nothing is wrong); every other outcome must say why,
        # and for PERMIT_WITH_CONDITIONS the reasons *are* the conditions.
        if decision["outcome"] == "PERMIT" and decision.get("reasons"):
            problems.append(f"a PERMIT carries reasons ({decision['reasons']})")
        if decision["outcome"] != "PERMIT" and not decision.get("reasons"):
            problems.append(f"{decision['outcome']} arrives with no reason and no condition")
        if problems:
            return _check("V-005", "decision envelope integrity", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        return _check("V-005", "decision envelope integrity", "PASS",
                      "closed-enum outcome, prohibitions pinned, obligations present")

    def _cross_consistency(self, b: dict) -> dict:
        profile, case = b.get("profile"), b.get("case") or {}
        disposition = b.get("disposition")
        problems = []
        if not profile:
            return _check("V-006", "cross-artefact consistency", "NOT_APPLICABLE", "no profile",
                          severity="MINOR")
        if case:
            if profile["case_id"] != case.get("case_id"):
                problems.append(f"profile case_id {profile['case_id']} != case {case.get('case_id')}")
            if profile["customer_id"] != case.get("customer_id"):
                problems.append(f"profile customer {profile['customer_id']} != case customer "
                                f"{case.get('customer_id')}")
            if case.get("customer_name") and \
                    profile["customer_360"]["legal_name"] != case["customer_name"]:
                problems.append(f"profile name {profile['customer_360']['legal_name']!r} != case "
                                f"name {case['customer_name']!r}")
        direct = round(sum(h["pct"] for h in profile["ownership"]["direct_holdings"]), 2)
        if abs(direct - 100.0) > 0.01:
            problems.append(f"direct holdings sum to {direct}%, not 100%")
        if disposition:
            ad = disposition.get("policy_admissibility") or {}
            if disposition.get("recommendation") and ad and \
                    disposition["recommendation"] not in ad.get("allowed_recommendations", []):
                problems.append(f"recommendation {disposition['recommendation']} is not in the band's "
                                f"allowed set {ad.get('allowed_recommendations')}")
            if disposition.get("finalised") is not False:
                problems.append("disposition is marked finalised")
            if (disposition.get("verification") or {}).get("verification_id") != b.get(
                    "verification", {}).get("verification_id") and b.get("verification"):
                problems.append("disposition cites a different verification report than the one "
                                "under review")
        if problems:
            return _check("V-006", "cross-artefact consistency", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        return _check("V-006", "cross-artefact consistency", "PASS",
                      "names, ownership and coverage agree across artefacts")

    def _coverage_honesty(self, b: dict) -> dict:
        profile, disposition = b.get("profile"), b.get("disposition") or {}
        if not profile:
            return _check("V-007", "gap disclosure", "NOT_APPLICABLE", "no profile", severity="MINOR")
        blocking = profile["coverage"]["blocking_rde_ids"]
        incomplete = not profile["screening"]["screening_complete"]
        if not blocking and not incomplete:
            return _check("V-007", "gap disclosure", "PASS",
                          "no blocking gaps and screening is complete")
        problems = []
        notes = " ".join(profile.get("data_quality", {}).get("notes", []))
        if blocking and not notes:
            problems.append("blocking gaps exist but the profile states no coverage figure")
        narrative = disposition.get("narrative")
        case = b.get("case", {}).get("case_id", "")
        if disposition and narrative:
            if blocking and not all(r in narrative for r in blocking):
                problems.append("the disposition narrative does not name the blocking gaps")
            if incomplete and "screening" not in narrative.lower():
                problems.append("screening is incomplete and the narrative does not say so")
        if problems:
            return _check("V-007", "gap disclosure", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        return _check("V-007", "gap disclosure", "PASS",
                      f"coverage {profile['coverage']['coverage_pct']}% and the screening gap are "
                      f"disclosed")

    def _obligations_acted(self, b: dict) -> dict:
        disposition = b.get("disposition") or {}
        obligations = {o["kind"] for o in disposition.get("obligations", [])}
        actions = disposition.get("actions") or []
        if not obligations:
            return _check("V-008", "obligations have actions", "NOT_APPLICABLE",
                          "no obligations in this bundle", severity="MINOR")
        covered = {a["obligation"] for a in actions}
        missing = sorted(obligations - covered)
        unknown = sorted(covered - obligations)
        incomplete = [a["obligation"] for a in actions
                      if not a.get("owner") or not a.get("due") or a.get("owner") == "UNASSIGNED"]
        problems = []
        if missing:
            problems.append(f"no action plan for {', '.join(missing)}")
        if unknown:
            problems.append(f"action plan cites obligations the policy did not impose: {unknown}")
        if incomplete:
            problems.append(f"actions without an owner or due date: {', '.join(incomplete)}")
        if problems:
            return _check("V-008", "obligations have actions", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        return _check("V-008", "obligations have actions", "PASS",
                      f"{len(obligations)} obligation(s), each with an owner and a due date")

    def _authority(self, b: dict) -> dict:
        """Approval requirements must not already carry a name; grants must name a human with a TTL;
        and nothing in the action plan may claim an agent performed a prohibited act."""
        rfi = b.get("rfi") or {}
        approval = b.get("approval") or rfi.get("approval") or {}
        disposition = b.get("disposition") or {}
        problems: list[str] = []

        if approval:
            granted = bool(approval.get("approved_at") or approval.get("state") == "APPROVED")
            if granted:
                if not approval.get("approver"):
                    problems.append("approval grant carries no named approver")
                if approval.get("approver") == approval.get("requested_by"):
                    problems.append("self-approval: requester and approver are the same actor")
                if approval.get("approver") in SELF_APPROVING_ACTORS:
                    problems.append(f"{approval['approver']} is not a human approver")
                if not approval.get("ttl_hours"):
                    problems.append("approval grant has no TTL")
            else:
                if approval.get("approver"):
                    problems.append(f"approval requirement already names an approver "
                                    f"({approval['approver']}) before any human acted")

        for action in disposition.get("actions") or []:
            authority = action.get("authority")
            if authority in PROHIBITED and action.get("status") != "AWAITING_HUMAN":
                problems.append(f"{action['obligation']} claims {authority} without human hand-off "
                                f"(status {action.get('status')})")
            if authority in GATED and action.get("status") == "COMPLETE":
                problems.append(f"{action['obligation']} claims a gated action completed")

        if disposition:
            if (disposition.get("approval") or {}).get("required") is not True:
                problems.append("disposition does not require human approval")
            if (disposition.get("approval") or {}).get("approver") is not None:
                problems.append("disposition names an approver before a human acted")

        if problems:
            return _check("V-009", "authority and approval", "FAIL", "; ".join(problems),
                          severity="BLOCKING")
        if not (rfi or disposition):
            return _check("V-009", "authority and approval", "NOT_APPLICABLE",
                          "no approval requirement in this bundle", severity="MINOR")
        return _check("V-009", "authority and approval", "PASS",
                      "no self-approval, no agent-named approver, gated actions handed to a human")

    def _language(self, b: dict) -> dict:
        texts = {
            "disposition": (b.get("disposition") or {}).get("narrative", ""),
            "rfi_body": (b.get("rfi") or {}).get("body", ""),
            "profile_notes": " ".join((b.get("profile") or {}).get("data_quality", {}).get("notes")
                                      or []),
        }
        hits = []
        for where, text in texts.items():
            for pat in TIPPING_OFF:
                if re.search(pat, text, re.I):
                    hits.append(f"tipping-off phrase {pat!r} in {where}")
            for pat in CLEARANCE_LANGUAGE:
                if re.search(pat, text, re.I):
                    hits.append(f"clearance language {pat!r} in {where}")
        if hits:
            return _check("V-010", "prohibited language", "FAIL", "; ".join(hits[:5]),
                          severity="BLOCKING")
        return _check("V-010", "prohibited language", "PASS",
                      "no tipping-off, no clearance claims, no reassurance")

    def _pii(self, b: dict) -> dict:
        rfi = b.get("rfi") or {}
        text = rfi.get("body", "") + " " + (b.get("disposition") or {}).get("narrative", "")
        hits = [kind for pat, kind in PII_PATTERNS if re.search(pat, text)]
        if hits:
            return _check("V-011", "PII minimisation in outbound text", "FAIL",
                          f"outbound text contains {', '.join(sorted(set(hits)))}",
                          severity="BLOCKING")
        return _check("V-011", "PII minimisation in outbound text", "PASS",
                      "no card, passport or bank details in outbound text")

    def _injection(self, b: dict) -> dict:
        """Detect instruction-like text that has leaked into an artefact. If it has, the artefact was
        built from untrusted text without the quarantine step."""
        blob = canonical({k: v for k, v in b.items() if k in
                          ("profile", "disposition", "rfi", "policy_decision")})
        hits = [label for pat, label in INJECTION_MARKERS if re.search(pat, blob, re.I)]
        if hits:
            return _check("V-012", "untrusted-content contamination", "FAIL",
                          f"artefacts contain instruction-like text: {', '.join(hits)}",
                          severity="BLOCKING")
        return _check("V-012", "untrusted-content contamination", "PASS",
                      "no instruction-like text in any artefact")

    def _freshness(self, b: dict) -> dict:
        profile = b.get("profile")
        if not profile:
            return _check("V-013", "profile freshness", "NOT_APPLICABLE", "no profile",
                          severity="MINOR")
        as_of = (b.get("as_of") or b.get("case", {}).get("as_of") or profile["as_of"])
        age = _days_between(profile["as_of"], as_of)
        limit = 30
        if age > limit:
            return _check("V-013", "profile freshness", "FAIL",
                          f"profile is {age} days old at the point of decision (limit {limit}); "
                          f"re-profile before relying on it", severity="BLOCKING")
        return _check("V-013", "profile freshness", "PASS", f"profile is {age} day(s) old")

    @staticmethod
    def _subjects(b: dict) -> list[str]:
        subjects = []
        if b.get("profile"):
            subjects.append(b["profile"].get("agent", "OPS-PROFILE"))
        if b.get("rfi"):
            subjects.append("OPS-OUTREACH")
        if b.get("disposition"):
            subjects.append(b["disposition"].get("agent", "OPS-DECIDE"))
        return subjects or ["OPS-PROFILE"]
