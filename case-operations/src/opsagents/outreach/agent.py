"""OPS-OUTREACH — the customer conversation.

The agent that talks to the customer is the one that can do the most damage, so the design is
deliberately narrow:

* it only asks for what a **blocking requirement** needs, and every question names the requirement it
  closes;
* it asks about **documents**, never about suspicion. Nothing in an outbound letter may let a customer
  infer that a disclosure is being considered (tipping-off), and nothing may offer reassurance
  ("no adverse findings") — that is a clearance claim whichever way it is meant;
* it **never sends**. `rfi.issue` is a gated action: the agent produces `READY_TO_SEND` and an approval
  requirement naming a human role and a TTL, and `send()` refuses anything that is not a genuine human
  grant;
* it accepts the policy's round budget (`rfi_max_rounds`), so a case that has exhausted its questions
  goes to a human rather than to a third letter.

Public entrypoints: `draft(...)`, `send(...)`, `assess(...)`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

from ..common.ledger import canonical, sha256_str
from ..common.policy import PolicyEngine

#: obligation -> the requirement a customer answer can close, and what to ask for.
#: An obligation that is not in this table is not a customer question: it is an internal action, and
#: the agent routes it to `internal_actions` instead of writing to the customer about it.
QUESTION_TEMPLATES = {
    "CLOSE_UBO_GAP": {
        "rde_id": "RDE-03",
        "question": ("Please provide a declaration of beneficial ownership identifying every natural "
                     "person who ultimately owns or controls 25% or more of the company, signed by a "
                     "director."),
        "documents": ["signed beneficial ownership declaration", "share register or cap table",
                      "certificate of incorporation (if not already provided)"],
        "customer_answerable": True,
    },
    "OBTAIN_SOURCE_OF_WEALTH": {
        "rde_id": "RDE-07",
        "question": ("Please provide evidence of the source of the funds used in the recent account "
                     "activity and of the origin of the principal shareholders' wealth, for example "
                     "sale contracts, audited accounts or a signed source of wealth statement."),
        "documents": ["signed source of wealth statement", "sale or supply contracts",
                      "audited financial statements"],
        "customer_answerable": True,
    },
    "OBTAIN_SOURCE_OF_FUNDS": {
        "rde_id": "RDE-05",
        "question": ("Please provide documentation for the origin of the funds received, such as "
                     "invoices, contracts and bank statements showing the payer."),
        "documents": ["invoices", "contracts", "counterparty bank statements"],
        "customer_answerable": True,
    },
    "EXPLAIN_ACTIVITY": {
        "rde_id": "RDE-04",
        "question": ("Please explain the change in the volume and pattern of payments on the account "
                     "compared with the activity described when the relationship was established, "
                     "and provide the underlying contracts."),
        "documents": ["written explanation signed by a director", "underlying contracts or invoices"],
        "customer_answerable": True,
    },
    "CONFIRM_INTRODUCER": {
        "rde_id": "RDE-14",
        "question": ("Please confirm the identity and role of the introducer who referred the "
                     "company and provide the introducer agreement."),
        "documents": ["introducer agreement", "introducer due diligence pack"],
        "customer_answerable": True,
    },
}

#: obligations that must never be put to a customer. They are the bank's own work.
INTERNAL_ONLY = {
    "COMPLETE_SCREENING": "complete screening against the unavailable lists",
    "HUMAN_EDD_APPROVAL": "obtain EDD approval from the EDD unit",
    "ESCALATE_MLRO": "escalate the sanctions adjudication to the MLRO",
    "REFRESH_SCREENING": "re-run screening when the list provider recovers",
}

#: Phrases that must not appear in outbound text. OPS-VERIFY enforces this with V-010; keeping the
#: list here as well means a bad draft fails at the agent that wrote it, with a better error.
FORBIDDEN_PHRASES = [
    r"\bsuspicious activity report\b", r"\bSAR\b", r"\bmoney laundering\b", r"\bwe suspect\b",
    r"\bthe authorities\b", r"\bpolice\b", r"\bFinCEN\b", r"\bNCA\b", r"\binvestigation\b",
    r"\bcleared\b", r"\bno adverse findings\b", r"\bfrozen\b", r"\bclose your account\b",
    r"\bterminate the relationship\b",
]

DEFAULT_BUSINESS_DAYS = 10

#: US federal holidays that can fall inside a ten-business-day window in the demo period. A real
#: deployment loads a calendar; the rule that matters is that the deadline is *business* days.
HOLIDAYS = {(2026, 11, 26), (2026, 12, 25), (2026, 10, 12)}


class OutreachRefused(RuntimeError):
    """Raised instead of producing a draft or a send that would breach a rule."""

    def __init__(self, reason: str, *, code: str = "REFUSED"):
        super().__init__(reason)
        self.code = code


def _add_business_days(start: date, days: int) -> date:
    d, added = start, 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5 and (d.year, d.month, d.day) not in HOLIDAYS:
            added += 1
    return d


class OutreachAgent:
    AGENT_ID = "OPS-OUTREACH"

    def __init__(self, ledger, policy: PolicyEngine):
        self.ledger = ledger
        self.policy = policy

    @staticmethod
    def _rfi_id(case: dict, rnd: int) -> str:
        """RFI-<case number>-<round>. The number, not the whole case id: the id is quoted to the
        customer and used as a filename, so it carries no separators that could escape either."""
        number = re.sub(r"[^0-9]", "", str(case.get("case_id", "")))
        return f"RFI-{number or '0000'}-{rnd}"

    # ── draft ───────────────────────────────────────────────────────────────
    def draft(self, *, case: dict, profile: dict, obligations: list[dict],
              prior_thread: dict | None = None, as_of: str | None = None) -> dict:
        """Compose the next request. Never sends; never exceeds the policy's round budget."""
        as_of_date = date.fromisoformat((as_of or case.get("as_of", "2026-10-21"))[:10])
        prior_thread = prior_thread or {"rounds_completed": 0, "messages": []}
        rnd = int(prior_thread.get("rounds_completed", 0)) + 1
        max_rounds = int(self.policy.pack()["thresholds"].get("rfi_max_rounds", 2))
        if rnd > max_rounds:
            self.ledger.rfi(event="REFUSED", rfi={"rfi_id": self._rfi_id(case, rnd),
                                                  "case_id": case["case_id"], "round": rnd,
                                                  "state": "REFUSED_ROUNDS_EXHAUSTED"})
            raise OutreachRefused(
                f"round {rnd} exceeds the policy budget of {max_rounds} rounds: this case goes to a "
                f"human, not to a third letter", code="ROUNDS_EXHAUSTED")

        questions, internal = self._derive_questions(obligations, profile)
        if not questions:
            raise OutreachRefused(
                "no customer-answerable gaps: this case needs internal action, not an RFI",
                code="NO_CUSTOMER_QUESTIONS")

        due = _add_business_days(as_of_date, DEFAULT_BUSINESS_DAYS)
        body = self._compose(case=case, profile=profile, questions=questions, due=due)
        self._assert_safe(body)

        rfi = {
            "rfi_id": self._rfi_id(case, rnd),
            "case_id": case["case_id"],
            "customer_id": profile["customer_id"],
            "round": rnd,
            "max_rounds": max_rounds,
            "state": "READY_TO_SEND",          # never SENT: that needs an approval
            "as_of": as_of_date.isoformat(),
            "due_date": due.isoformat(),
            "subject": f"Information required to complete our review of your account "
                       f"({case['case_id']})",
            "body": body,
            "message_hash": self._hash(body),
            "questions": questions,
            "internal_actions": internal,
            "covers_rde_ids": sorted({q["rde_id"] for q in questions}),
            "authority_action": "rfi.issue",
            "approval": {
                "required": True,
                "action": "rfi.issue",
                "requested_by": self.AGENT_ID,
                "approver_role": "CASE_ANALYST",
                "ttl_hours": 24,
                "approver": None,           # filled in only by a human, only in send()
                "approved_at": None,
                "state": "PENDING",
            },
            "language": {"locale": "en-GB", "reading_level": "plain",
                         "tipping_off_check": "enforced by OPS-VERIFY V-010"},
        }
        rfi["evidence_id"] = self.ledger.rfi(event="DRAFTED", rfi=rfi)
        return rfi

    # ── send (gated) ────────────────────────────────────────────────────────
    def send(self, rfi: dict, approval: dict) -> dict:
        """Transition to SENT. Only a named human with a live TTL can authorise this."""
        if rfi.get("state") != "READY_TO_SEND":
            raise OutreachRefused(f"cannot send an RFI in state {rfi.get('state')!r}",
                                  code="BAD_STATE")
        approver = (approval or {}).get("approver")
        if not approver:
            raise OutreachRefused("rfi.issue requires a named approver; the agent cannot send",
                                  code="NO_APPROVER")
        if approver == self.AGENT_ID or approver.startswith("OPS-"):
            raise OutreachRefused(f"{approver} is not a human approver", code="AGENT_APPROVER")
        if approver == rfi["approval"]["requested_by"]:
            raise OutreachRefused("self-approval is not permitted", code="SELF_APPROVAL")
        if int((approval or {}).get("ttl_hours") or 0) <= 0:
            raise OutreachRefused("approval has no live TTL", code="EXPIRED_APPROVAL")
        if self.policy.authority("rfi.issue")["decision"] != "approval_required":
            raise OutreachRefused("policy does not classify rfi.issue as gated — refusing to proceed",
                                  code="AUTHORITY_DRIFT")
        sent = {**rfi, "state": "SENT",
                "approval": {**rfi["approval"], **approval, "state": "APPROVED"}}
        sent["evidence_id"] = self.ledger.rfi(event="SENT", rfi=sent)
        return sent

    # ── assess a response ───────────────────────────────────────────────────
    def assess(self, *, rfi: dict, response: dict, profile: dict) -> dict:
        """Judge what came back. A document counts only if it is present, signed, dated and usable."""
        by_rde: dict[str, list[dict]] = {}
        for doc in response.get("documents", []):
            for rde in doc.get("rde_ids", []):
                by_rde.setdefault(rde, []).append(doc)

        updates, red_flags = [], []
        for q in rfi["questions"]:
            docs = by_rde.get(q["rde_id"], [])
            usable = [d for d in docs if d.get("signed") and d.get("dated")
                      and not d.get("content_flags")]
            for d in docs:
                if not d.get("signed"):
                    red_flags.append(f"{d['kind']} is unsigned")
                if not d.get("dated"):
                    red_flags.append(f"{d['kind']} is undated")
                for flag in d.get("content_flags") or []:
                    red_flags.append(f"{d['kind']}: {flag}")
            if usable:
                status = "SATISFIED"
            elif docs:
                status = "PARTIAL"
            else:
                status = "MISSING"
            updates.append({"rde_id": q["rde_id"], "obligation": q["obligation"],
                            "status": status, "documents_received": len(docs),
                            "basis": (f"{len(usable)} usable document(s)" if usable else
                                      ("received but defective or incomplete" if docs
                                       else "no document received"))})

        satisfied = [u["rde_id"] for u in updates if u["status"] == "SATISFIED"]
        outstanding = [u["rde_id"] for u in updates if u["status"] != "SATISFIED"]
        outcome = ("SATISFIED" if not outstanding
                   else "PARTIAL" if satisfied else "INSUFFICIENT")
        rounds_used = int(rfi["round"])
        exhausted = rounds_used >= int(rfi["max_rounds"])
        next_action = ("REASSESS_AND_DECIDE" if outcome == "SATISFIED"
                       else "ESCALATE_ROUNDS_EXHAUSTED" if exhausted else "FOLLOW_UP")
        assessment = {
            "rfi_id": rfi["rfi_id"], "case_id": rfi["case_id"], "round": rounds_used,
            "max_rounds": rfi["max_rounds"],
            "received_at": response.get("received_at"),
            "outcome": outcome,
            "satisfied_rde_ids": satisfied,
            "outstanding_rde_ids": outstanding,
            "rde_updates": updates,
            "red_flags": sorted(set(red_flags)),
            "rounds_used": rounds_used,
            "next_action": next_action,
            "profile_must_be_refreshed": bool(updates),
            "state": "ROUNDS_EXHAUSTED" if exhausted and outcome != "SATISFIED" else "ASSESSED",
        }
        assessment["evidence_id"] = self.ledger.rfi(event="RESPONSE_ASSESSED",
                                                    rfi={**rfi, "state": "RESPONSE_ASSESSED"})
        return assessment

    # ── internals ───────────────────────────────────────────────────────────
    def _derive_questions(self, obligations: list[dict], profile: dict) -> tuple[list, list]:
        blocking = set(profile["coverage"]["blocking_rde_ids"])
        missing = set(profile["coverage"]["missing_rde_ids"])
        questions, internal = [], []
        seen_rde = set()

        for ob in obligations:
            kind = ob["kind"] if isinstance(ob, dict) else str(ob)
            template = QUESTION_TEMPLATES.get(kind)
            if template is None:
                if kind in INTERNAL_ONLY:
                    internal.append({"obligation": kind, "action": INTERNAL_ONLY[kind],
                                     "customer_facing": False})
                else:
                    internal.append({"obligation": kind,
                                     "action": "unmapped obligation — route to the case analyst",
                                     "customer_facing": False})
                continue
            # only ask when the requirement is actually open for this customer
            if template["rde_id"] not in blocking | missing and profile["coverage"]["coverage_pct"] >= 95:
                continue
            if template["rde_id"] in seen_rde:
                continue
            seen_rde.add(template["rde_id"])
            questions.append({"rde_id": template["rde_id"], "obligation": kind,
                              "question": template["question"],
                              "documents": template["documents"],
                              "closes_gap": template["rde_id"] in blocking})
        questions.sort(key=lambda q: q["rde_id"])
        return questions, internal

    def _compose(self, *, case: dict, profile: dict, questions: list[dict], due: date) -> str:
        name = profile["customer_360"]["legal_name"]
        lines = [
            f"Dear {name},",
            "",
            "As part of our ongoing review of your account, we need some additional information "
            "about your business and the activity on the account. Please provide the following:",
            "",
        ]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q['question']}")
            lines.append(f"   Documents that would be helpful: {', '.join(q['documents'])}.")
            lines.append("")
        lines += [
            f"Please send these documents by {due.strftime('%d %B %Y')}. If anything is unclear, or "
            "you need more time, contact your relationship manager and we will help.",
            "",
            "We are required to keep our records up to date for all customers, and we appreciate "
            "your help with this.",
            "",
            "Yours sincerely,",
            "Financial Crime Operations",
        ]
        return "\n".join(lines)

    def _assert_safe(self, body: str) -> None:
        for pattern in FORBIDDEN_PHRASES:
            if re.search(pattern, body, re.I):
                raise OutreachRefused(
                    f"draft contains prohibited language ({pattern}): a customer must not learn that "
                    f"a disclosure is being considered", code="TIPPING_OFF")
        if re.search(r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b", body):
            raise OutreachRefused("draft contains what looks like a card number", code="PII")
        if re.search(r"\b\d{2}\s?[A-Z]{3}\s?\d{4}\b", body):
            raise OutreachRefused("draft contains what looks like a passport number", code="PII")

    @staticmethod
    def _hash(body: str) -> str:
        return sha256_str(body)
