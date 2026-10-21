"""OPS-DECIDE — composes a recommendation for a human, inside the policy's answer.

This agent has no risk model, no thresholds and no matrix. Every one of those lives in the
**AXIOM-POLICY** bundle, and the agent reaches it through three calls:

    data.agent.decision.result     the envelope: score, band, CDD level, outcome, gates, obligations
    data.agent.disposition.result  is *this* recommendation admissible for *this* band?
    data.agent.authority.result    may this action be taken at all?

Two consequences follow, and they are the point of the whole design:

* if the policy service is unavailable the agent **stops** — an unreachable policy service is an
  incident, not an invitation to reason;
* if the critic (OPS-VERIFY) failed the bundle the agent **returns the case for rework** instead of
  recommending anything, and it forwards `critic_status` into the policy input so the policy's own
  gates fire even if the agent were somehow bypassed.

Public entrypoint: `decide(...) -> disposition`.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..common.ledger import canonical, sha256_str
from ..common.policy import PolicyEngine, PolicyUnavailable

#: envelope outcome -> the recommendation we would like to make, most severe first.
#: The policy decides whether the band admits it; this table only expresses intent.
PREFERRED = {
    "SANCTIONS_ADJUDICATION": "REFER_MLRO",
    "VERY_HIGH": "REFER_MLRO",
    "BLOCK_HIGH": "ESCALATE_EDD",
    "BLOCK": "HOLD_FOR_RFI",
    "REQUIRE_EDD": "ESCALATE_EDD",
    "REQUIRE_RFI": "HOLD_FOR_RFI",
    "PERMIT_WITH_CONDITIONS": "PROCEED_WITH_CONDITIONS",
    "PERMIT": "PROCEED",
}

#: when the preferred recommendation is not in the band's matrix, walk down this order and take the
#: first admissible one. Recorded as `downgraded_from` so nobody has to guess why it changed.
CONSERVATIVE_ORDER = ["REFER_MLRO", "ESCALATE_EDD", "HOLD_FOR_RFI", "PROCEED_WITH_CONDITIONS",
                      "PROCEED"]

#: policy route -> the human role that must approve.
ROUTE_TO_APPROVER = {
    "MLRO": "MLRO",
    "EDD_UNIT": "EDD_UNIT_HEAD",
    "COMPLIANCE_OFFICER": "COMPLIANCE_OFFICER",
    "CASE_ANALYST": "CASE_ANALYST",
}

#: obligation -> (owner, authority action, business-day SLA). An obligation with no row here is
#: emitted as UNASSIGNED rather than dropped: unmapped work must be visible.
OBLIGATION_ACTIONS = {
    "CLOSE_UBO_GAP": ("CASE_ANALYST", "rfi.issue", 10,
                      "request and verify a beneficial ownership declaration"),
    "OBTAIN_SOURCE_OF_WEALTH": ("CASE_ANALYST", "rfi.issue", 10,
                                "obtain and verify source of wealth evidence"),
    "OBTAIN_SOURCE_OF_FUNDS": ("CASE_ANALYST", "rfi.issue", 10,
                               "obtain source of funds documentation"),
    "COMPLETE_SCREENING": ("SCREENING_OPS", "internal.read", 2,
                           "complete screening against the unavailable lists"),
    "HUMAN_EDD_APPROVAL": ("EDD_UNIT", "case.finalise", 5,
                           "EDD unit to review and approve the enhanced due diligence file"),
    "ESCALATE_MLRO": ("MLRO", "sanctions.clear", 1,
                      "MLRO to adjudicate the sanctions match"),
    "EXPLAIN_ACTIVITY": ("CASE_ANALYST", "rfi.issue", 10,
                         "obtain an explanation and evidence for the activity change"),
    "CONFIRM_INTRODUCER": ("CASE_ANALYST", "rfi.issue", 10,
                           "confirm the introducer and obtain the introducer agreement"),
}


def _status_detail(kind: str, status: str, owner: str, rfi: dict | None,
                   assessment: dict | None, rounds_left: int) -> str:
    """One sentence saying why the action sits where it sits. An action plan whose statuses are
    unexplained is a spreadsheet, not an instruction to a named owner."""
    if status == "BLOCKED_NO_RFI":
        return (f"{kind} needs customer evidence, but the policy round budget is spent: no request "
                f"can be raised, so {owner} must place this in front of the approver instead. "
                f"OPS-OUTREACH has no round left to draft.")
    if status == "OPEN" and rfi is None:
        return (f"{kind} needs customer evidence and no request is open: OPS-OUTREACH must raise "
                f"before the due date ({rounds_left} left in the policy budget), then {owner} "
                f"tracks the answer.")
    if status == "AWAITING_CUSTOMER":
        return (f"{kind} is with the customer under {rfi['rfi_id']} (round {rfi['round']}); "
                f"due {rfi['due_date']}. {owner} owns the follow-up.")
    if status == "EVIDENCE_RECEIVED":
        outcome = (assessment or {}).get("outcome", "PARTIAL")
        return (f"evidence received and assessed {outcome}: {owner} must re-assess and decide, "
                f"not reuse the previous disposition.")
    if status == "AWAITING_HUMAN":
        return f"{kind} is a human decision: {owner} acts; the agent may only hand it over."
    return f"{kind} is assigned to {owner}; the agent cannot execute it."


def _add_business_days(start: date, days: int) -> date:
    d, added = start, 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


class DecisionAgent:
    AGENT_ID = "OPS-DECIDE"

    def __init__(self, ledger, policy: PolicyEngine, verifier=None):
        self.ledger = ledger
        self.policy = policy
        # optional: a verifier lets `propose` re-verify its own output before it is offered (see
        # `propose(post_check=True)`); the decision is not permitted to certify itself, so the
        # verification it runs is the same OPS-VERIFY battery applied to the bundle it produced
        self.verifier = verifier

    # ── policy input ────────────────────────────────────────────────────────
    def policy_input(self, *, case: dict, profile: dict, verification: dict,
                     search_pack: dict | None = None) -> dict:
        """Alias used by the CLI and the tests: the pinned document handed to the policy service."""
        return self.build_policy_input(case=case, profile=profile, verification=verification,
                                       search_pack=search_pack)

    def build_policy_input(self, *, case: dict, profile: dict, verification: dict,
                           search_pack: dict | None = None) -> dict:
        """Exactly the shape `data.agent.decision.result` expects — no more, no less.

        Every factor id is validated against the pack's catalogue: an id the policy does not define
        is a hard error here, because silently dropping it would lower a score.
        """
        catalogue = self.policy.factor_catalog()
        factors = {k: bool(v) for k, v in profile["factors"].items()}
        unknown = sorted(set(factors) - set(catalogue))
        if unknown:
            raise ValueError(f"undeclared risk factors: {unknown} (policy {self.policy.version})")

        return {
            "case": {"case_id": case["case_id"],
                     "cdd_level_requested": case.get("cdd_level_requested", "CDD"),
                     "jurisdiction_profile": case.get("jurisdiction_profile", "EU_FATF")},
            "input_hash": sha256_str(canonical({
                "case": case["case_id"], "profile": profile["profile_id"],
                "verification": verification.get("verification_id")})),
            "evaluate_at": case.get("as_of", profile["as_of"]),
            "factors": factors,
            "risk": {"cdd_level": profile["internal_risk"].get("cdd_level")
                     or case.get("cdd_level_requested", "CDD")},
            "screening": {"hits": profile["screening"]["hits"],
                          "queries": [{"list": l} for l in profile["screening"]["lists_queried"]],
                          "lists_unavailable": profile["screening"]["lists_unavailable"]},
            "ownership": {"paths": profile["ownership"]["paths"],
                          "unresolved": profile["ownership"]["unresolved"],
                          "direct_holdings": profile["ownership"]["direct_holdings"]},
            "coverage": {"elements": [{"rde_id": e["rde_id"], "status": e["status"]}
                                      for e in profile["coverage"]["elements"]]},
            "critic_status": verification["critic_status"],
            "injection_attempt": bool(verification.get("injection_attempt")),
            "activity": {"declared_vs_observed_pct": profile["transactions"]["declared_vs_observed_pct"],
                         "peer_percentile": profile["transactions"]["peer_percentile"],
                         "top_counterparty_hhi": profile["transactions"]["top_counterparty_hhi"]},
            "search": {"status": (search_pack or {}).get("status"),
                       "gaps": [g.get("code") for g in (search_pack or {}).get("gaps", [])]},
        }

    # ── decide ──────────────────────────────────────────────────────────────
    def propose(self, *, case: dict, profile: dict, verification: dict,
                rfi: dict | None = None, assessment: dict | None = None,
                search_pack: dict | None = None) -> dict:
        """Offer a disposition. `propose`, never `decide`: the recommendation is the agent's, the
        outcome is the policy's, and the finalisation is a human's."""
        policy_input = self.build_policy_input(case=case, profile=profile,
                                               verification=verification, search_pack=search_pack)
        try:
            envelope = self.policy.decide(policy_input)
        except PolicyUnavailable:
            self.ledger.event(event="POLICY_UNAVAILABLE", detail={"case_id": case["case_id"]})
            raise

        base = {
            "case_id": case["case_id"],
            "customer_id": profile["customer_id"],
            "agent": self.AGENT_ID,
            "as_of": case.get("as_of", profile["as_of"]),
            "profile_id": profile["profile_id"],
            "policy": {
                "decision_id": envelope["decision_id"],
                "policy_version": envelope["policy_version"],
                "outcome": envelope["outcome"],
                "reasons": envelope["reasons"],
                "route": envelope["gates"]["route_to"],
                "required_gates": envelope["gates"]["required_gates"],
                "risk": envelope["risk"],
                # the pack, not the agent, owns this: `gates.auto_final_allowed` is false and the
                # disposition schema pins it to false, so a pack that tried to grant
                # self-finalisation would fail validation instead of being obeyed
                "auto_final_allowed": bool(self.policy.pack()["gates"]["auto_final_allowed"]),
                "agent_may_clear_sanctions_hit": envelope["agent_may_clear_sanctions_hit"],
            },
            "verification": {"verification_id": verification["verification_id"],
                             "critic_status": verification["critic_status"],
                             "blocking_check_ids": verification.get("blocking_check_ids", [])},
            "obligations": envelope["obligations"],
        }

        # 0. Ask the policy whether the band would even admit the recommendation we would have
        #    made. This happens before the critic branch on purpose: a returned-for-rework case must
        #    still show what the policy said, otherwise the rework has no target.
        proposed = self._preferred_recommendation(envelope)
        admissible = self.policy.dispose(band=envelope["risk"]["band"], recommendation=proposed,
                                         required_gates=envelope["gates"]["required_gates"],
                                         critic_status=verification["critic_status"])

        # 1. A failed critic is not a decision. Return the case: the evidence cannot be relied on,
        #    and no amount of policy arithmetic makes unverified evidence decideable.
        if verification["critic_status"] != "PASS":
            disposition = {**base,
                           "disposition_id": self._disposition_id(base, "RETURNED_FOR_REWORK"),
                           "state": "RETURNED_FOR_REWORK",
                           "recommendation": None,
                           "recommendation_proposed": None,
                           "policy_admissibility": {
                               "admissible": admissible["admissible"],
                               "reason": admissible["reason"],
                               "allowed_recommendations": admissible["allowed_recommendations"],
                               "downgraded_from": None,
                               "human_approval_required": admissible["human_approval_required"],
                               "agent_may_finalise": admissible["agent_may_finalise"]},
                           "actions": [],
                           "approval": {"required": True, "approver_role": None,
                                        "approver": None, "state": "NOT_REQUESTED",
                                        "blocked_by": verification.get("blocking_check_ids", []),
                                        "may_be_approved_while_blocked": False},
                           "finalised": False,
                           "narrative": self._rework_narrative(verification),
                           "evidence_ids": [i for i in (profile.get("evidence_id"),
                                                        verification.get("evidence_id")) if i],
                           }
            disposition["evidence_id"] = self.ledger.disposition(disposition=disposition)
            return disposition

        # 2. Choose a recommendation the band actually admits.
        recommendation, downgraded_from = proposed, None
        if not admissible["admissible"]:
            for candidate in CONSERVATIVE_ORDER:
                check = self.policy.dispose(band=envelope["risk"]["band"],
                                            recommendation=candidate,
                                            required_gates=envelope["gates"]["required_gates"],
                                            critic_status=verification["critic_status"])
                if check["admissible"]:
                    recommendation, downgraded_from = candidate, proposed
                    admissible = check
                    break
        if not admissible["admissible"]:
            # The matrix has no admissible recommendation for this band. Refuse: manufacturing one
            # would be the agent deciding, which it does not do.
            raise PolicyUnavailable(
                f"no recommendation is admissible for band {envelope['risk']['band']}; refusing to "
                f"manufacture a disposition")

        actions = self._action_plan(envelope["obligations"], case=case, rfi=rfi,
                                    assessment=assessment)
        approval = {
            "required": True,                              # every band, always
            "approver_role": ROUTE_TO_APPROVER.get(envelope["gates"]["route_to"],
                                                   "COMPLIANCE_OFFICER"),
            "route": envelope["gates"]["route_to"],
            "approver": None,
            "approved_at": None,
            "state": "PENDING",
            "blocked_by": [g for g in envelope["gates"]["required_gates"]],
            "may_be_approved_while_blocked": False,
            "approval_reason": admissible.get("approval_reason"),
        }
        state = ("AWAITING_RFI" if (rfi and rfi.get("state") in ("SENT", "READY_TO_SEND")
                                    and "HOLD_FOR_RFI" == recommendation)
                 else "PENDING_APPROVAL")
        disposition = {**base,
                       "disposition_id": self._disposition_id(base, recommendation),
                       "state": state,
                       "recommendation": recommendation,
                       "recommendation_proposed": proposed,
                       "policy_admissibility": {
                           "admissible": admissible["admissible"],
                           "reason": admissible["reason"],
                           "allowed_recommendations": admissible["allowed_recommendations"],
                           "downgraded_from": downgraded_from,
                           "human_approval_required": admissible["human_approval_required"],
                           "agent_may_finalise": admissible["agent_may_finalise"]},
                       "actions": actions,
                       "approval": approval,
                       "finalised": False,
                       "outreach": (None if not rfi else
                                    {"rfi_id": rfi["rfi_id"], "round": rfi["round"],
                                     "state": rfi["state"], "due_date": rfi["due_date"],
                                     "assessment": (None if not assessment else
                                                    assessment["outcome"])}),
                       "narrative": self._narrative(envelope, profile, verification, recommendation,
                                                    proposed, downgraded_from, actions, approval),
                       "evidence_ids": [i for i in (profile.get("evidence_id"),
                                                    verification.get("evidence_id"),
                                                    (rfi or {}).get("evidence_id")) if i],
                       }
        disposition["evidence_id"] = self.ledger.disposition(disposition=disposition)
        return disposition

    # ── re-decision ─────────────────────────────────────────────────────────
    @staticmethod
    def diff(previous: dict, current: dict) -> list[dict]:
        """What moved between two runs on the same case. A silent change is a defect, so it is
        named: recommendation, outcome, band, critic status, reasons that appeared or disappeared."""
        changes = []
        named = {"recommendation": "RECOMMENDATION_MOVED", "state": "STATE_MOVED"}
        for field_name in ("recommendation", "state"):
            if previous.get(field_name) != current.get(field_name):
                changes.append({"kind": named[field_name], "field": field_name,
                                "from": previous.get(field_name), "to": current.get(field_name)})
        for path, label in ((("policy", "outcome"), "OUTCOME_MOVED"),
                            (("policy", "risk", "band"), "BAND_MOVED"),
                            (("verification", "critic_status"), "CRITIC_MOVED")):
            a, b = previous, current
            for key in path:
                a = (a or {}).get(key) if isinstance(a, dict) else None
                b = (b or {}).get(key) if isinstance(b, dict) else None
            if a != b:
                changes.append({"kind": label, "field": ".".join(path), "from": a, "to": b})
        pa = set((previous.get("policy") or {}).get("reasons", []))
        pb = set((current.get("policy") or {}).get("reasons", []))
        for reason in sorted(pb - pa):
            changes.append({"kind": "REASON_APPEARED", "reason": reason})
        for reason in sorted(pa - pb):
            changes.append({"kind": "REASON_DISAPPEARED", "reason": reason})
        return changes

    # ── internals ───────────────────────────────────────────────────────────
    # the tests and the spec refer to the "desired" recommendation: the one the band and the
    # envelope suggest before admissibility is checked
    _desired_recommendation = _preferred_recommendation = staticmethod(
        lambda envelope: DecisionAgent._preferred(envelope))

    @staticmethod
    def _preferred(envelope: dict) -> str:
        reasons = set(envelope["reasons"])
        band = envelope["risk"]["band"]
        if "SANCTIONS_HIT_REQUIRES_ADJUDICATION" in reasons:
            return PREFERRED["SANCTIONS_ADJUDICATION"]
        if band == "VERY_HIGH":
            return PREFERRED["VERY_HIGH"]
        if envelope["outcome"] in ("BLOCK", "REQUIRE_EDD") and band == "HIGH":
            return PREFERRED["BLOCK_HIGH"]
        return PREFERRED.get(envelope["outcome"], "HOLD_FOR_RFI")

    def _action_plan(self, obligations: list[dict], *, case: dict, rfi: dict | None,
                     assessment: dict | None) -> list[dict]:
        start = date.fromisoformat((case.get("as_of") or "2026-10-21")[:10])
        rde_status = {u["rde_id"]: u["status"] for u in (assessment or {}).get("rde_updates", [])}
        max_rounds = int(self.policy.pack()["thresholds"].get("rfi_max_rounds", 2))
        rounds_left = max(0, max_rounds - int(case.get("prior_rfi_rounds", 0)) - (1 if rfi else 0))
        actions = []
        for ob in obligations:
            kind, because = ob["kind"], ob.get("because", "")
            spec = OBLIGATION_ACTIONS.get(kind)
            if spec is None:
                actions.append({"obligation": kind, "because": because, "owner": "UNASSIGNED",
                                "action": "unmapped obligation — policy-to-process gap",
                                "authority": None, "due": None,
                                "status": "NEEDS_PROCESS_MAPPING",
                                "detail": f"the policy imposed {kind} and no process owner is mapped "
                                          f"for it; nothing will happen to it until one is"})
                continue
            owner, authority, sla, text = spec
            status = "OPEN"
            rfi_id = None
            if authority == "rfi.issue":
                if rfi is None:
                    # no request is open: either a round is still available (raise one) or the
                    # policy budget is spent and the gap has to go to a human
                    status = "OPEN" if rounds_left > 0 else "BLOCKED_NO_RFI"
                else:
                    rfi_id = rfi["rfi_id"]
                    status = ("AWAITING_CUSTOMER" if rfi.get("state") in ("READY_TO_SEND", "SENT")
                              else "OPEN")
                    if assessment and assessment.get("outcome") == "SATISFIED":
                        status = "EVIDENCE_RECEIVED"
            if authority in ("case.finalise", "sanctions.clear"):
                # an obligation whose execution is a human decision: the agent can only hand it over
                status = "AWAITING_HUMAN"
            actions.append({"obligation": kind, "because": because, "owner": owner,
                            "action": text, "authority": authority,
                            "due": _add_business_days(start, sla).isoformat(),
                            "status": status, "rfi_id": rfi_id,
                            "detail": _status_detail(kind, status, owner, rfi, assessment,
                                                     rounds_left)})
        return actions

    @staticmethod
    def _disposition_id(base: dict, recommendation: str) -> str:
        return "DSP-" + sha256_str(canonical({
            "case": base["case_id"], "decision": base["policy"]["decision_id"],
            "profile": base["profile_id"], "rec": recommendation}))[7:15]

    @staticmethod
    def _rework_narrative(verification: dict) -> str:
        ids = ", ".join(verification.get("blocking_check_ids") or []) or "none named"
        return (
            "No recommendation is made on this case. The independent verification of the case "
            f"artefacts failed (critic_status FAIL; blocking checks: {ids}), so the evidence cannot "
            "be relied on and the case is returned for rework. The policy envelope has been "
            "computed and is recorded, but a disposition on unverified evidence would be a decision "
            "made on numbers nobody has checked. Re-run the failing agent, re-verify, and the case "
            "will be decided on the corrected artefacts. Automatic finalisation is not available and "
            "the agent may not clear a sanctions match.")

    @staticmethod
    def _narrative(envelope: dict, profile: dict, verification: dict, recommendation: str,
                   proposed: str, downgraded_from: str | None, actions: list[dict],
                   approval: dict) -> str:
        risk = envelope["risk"]
        obligations = ", ".join(o["kind"] for o in envelope["obligations"]) or "none"
        blocking = ", ".join(profile["coverage"]["blocking_rde_ids"]) or "none"
        floor = ""
        if risk.get("weighted_score_band") and risk["weighted_score_band"] != risk["band"]:
            floor = (f" A band floor raised the band above the weighted score "
                     f"(weighted band {risk['weighted_score_band']}).")
        downgrade = (f" The preferred recommendation {downgraded_from} is not admissible for this "
                     f"band, so the recommendation has been stepped down to {recommendation}."
                     if downgraded_from else "")
        awaiting = [a["obligation"] for a in actions if a["status"].startswith("AWAITING")]
        return (
            f"Policy outcome {envelope['outcome']} for {profile['customer_360']['legal_name']} "
            f"({envelope['decision_id']}). Risk score {risk['score']}, band {risk['band']}, CDD "
            f"level {risk['cdd_level']}.{floor} Reasons: {', '.join(envelope['reasons']) or 'none'}. "
            f"Evidence coverage {profile['coverage']['coverage_pct']}% with decision-blocking gaps "
            f"{blocking}; screening complete: {profile['screening']['screening_complete']}. "
            f"Obligations: {obligations}. Verification: {verification['critic_status']} "
            f"({verification['counts']['checks']} checks, {verification['counts']['fail']} failed). "
            f"Recommended disposition: {recommendation}.{downgrade}"
            + (f" Awaiting human action on: {', '.join(awaiting)}." if awaiting else "")
            + f" Approval required from {approval['approver_role']}. Automatic finalisation is not "
              f"available and the agent may not clear a sanctions match.")
