# OPS case operations — four agents, one runtime, one ledger, one policy service

`case-operations/` is the operational half of the stack: the four agents that actually run a case
from raw internal data to a recommendation on a human's desk.

```
OPS-PROFILE ──► OPS-VERIFY ──► OPS-DECIDE ◄── AXIOM-POLICY (policy service)
   internal         attacks        composes        risk · band · gates · outcome
   facts            the outputs    the envelope    obligations · admissibility · authority
        │                │              │
        └── OPS-OUTREACH ┘              └── approval requirement (a human, always)
            customer RFI
```

| Agent | Question it answers | Cannot |
|---|---|---|
| **OPS-PROFILE** | what do our own systems say about this customer, evidenced? | score, decide, contact anyone |
| **OPS-VERIFY** | is the work of the other three internally true and safe? | gather facts, decide, clear anything |
| **OPS-OUTREACH** | what must we ask the customer, and did their answer close the gap? | send without a named human approver |
| **OPS-DECIDE** | what does the policy say, and what should a human approve? | decide, finalise, or clear a sanctions hit |

Everything here runs: `make demo` builds two real cases end to end, crashes a run on purpose,
resumes it, verifies the hash chain and checks every contract.

## Quick start

```bash
cd case-operations
make run                    # CASE-4831: profile → verify → RFI → decide
make run CASE=CASE-4902     # the clean case
make ledger                 # verify the evidence chain
make kill && make resume    # crash at API call 5, then resume from the checkpoint
make test                   # 53 acceptance tests
make contracts              # schemas, prompt pins, ledger, policy service
make demo                   # all of the above
```

No network, no internal connectivity: the bank's systems are served by `mock/api/` — ten fixtures
with realistic latency, pagination, a 503 on first attempt and a 429 after two retries, so retry and
evidence behaviour is exercised rather than assumed.

## What the two cases produce

**CASE-4831 · Meridian Trade Holdings Ltd** — AE metals trader, remotely onboarded, introduced by a
Gulf introducer, director is a Venezuelan PEP, 45% held through an undeclared nominee, turnover 2.4×
declared, counterparty incorporated five months ago with no employees and no premises.

```
profile        PRF-CUST-1001-169b6ad8   13 factors   coverage 30.0%
verification   PASS (9 pass, 0 fail, 4 n/a)
policy         BLOCK   band HIGH (weighted MEDIUM — a band floor applied)   route EDD_UNIT
recommendation ESCALATE_EDD   admissible=True   approver EDD_UNIT_HEAD
outreach       RFI-4831-2 round 2/2 READY_TO_SEND   questions RDE-03/04/07/19
obligations    OBTAIN_SOURCE_OF_WEALTH · CLOSE_UBO_GAP · COMPLETE_SCREENING · HUMAN_EDD_APPROVAL
state          PENDING_APPROVAL   finalised: false
```

**CASE-4902 · Nordwind Logistics GmbH** — DE logistics, clean control chain, everything satisfied:

```
profile        factors 1   coverage 100.0%
policy         PERMIT_WITH_CONDITIONS   band LOW   route COMPLIANCE_OFFICER
recommendation PROCEED_WITH_CONDITIONS  approver COMPLIANCE_OFFICER
outreach       none — the only obligation (COMPLETE_SCREENING) is internal
```

The contrast is the point: same pipeline, opposite conclusions, and in both cases the recommendation
is a *proposal* (`finalised: false`, no approver name) rather than a decision.

## Layout

```
src/opsagents/
  common/ledger.py     hash-chained, signed, append-only evidence + idempotency store
  common/mockapi.py    the internal APIs, with latency, pagination, retries, failures
  common/policy.py     the OPA client — decide / dispose / authority / factor catalog
  common/runs.py       checkpoints, budgets, run state, kill switch
  profile/agent.py     OPS-PROFILE       verification/agent.py  OPS-VERIFY
  outreach/agent.py    OPS-OUTREACH      decision/agent.py      OPS-DECIDE
  cli.py               run · profile · verify · rfi · decide · ledger · show
mock/api/              10 fixtures · 2 cases · 5 counterparties · RFI thread · document set
spec/                  00 definition · 01 system prompts · 02 skills · 03 I/O · 04 evidence
                       05 long-running · 06 guardrails · 07 evaluation
prompts/               system.md + one role prompt per agent, hash-pinned in pins.json
schemas/               7 JSON Schemas, every artefact validated in `make contracts`
config/                runtime thresholds, tool grants, model routing
tests/                 53 acceptance tests across the four agents + integration + resumability
scripts/check_contracts.py
```

## The five properties that make this credible

1. **The policy service is the only decision truth.** OPS-DECIDE has no risk model, no thresholds and
   no matrix. It calls `data.agent.decision.result`, then `data.agent.disposition.result` to ask
   whether the recommendation it wants is even admissible for the band, and walks down
   `REFER_MLRO → ESCALATE_EDD → HOLD_FOR_RFI → PROCEED_WITH_CONDITIONS → PROCEED` when it is not. A
   recommendation outside the band's matrix is never presented.
2. **A recommendation is not a decision.** `finalised: false`, `agent_may_finalise: false`,
   `approver: null`, `human_approval_required: true` — and no agent id can appear in an `approver`
   field, which OPS-VERIFY's V-009 checks on every run.
3. **The critic changes the outcome, not the commentary.** `critic_status` goes back into the policy
   service, where `CRITIC_FAIL` forces a gate; a failed verification moves the case to
   `RETURNED_FOR_REWORK`. A PASS lists the thirteen checks that ran; an empty report is a failure.
4. **Every number is re-derivable.** Coverage, HHI, deviation, unresolved ownership and the factor map
   are recomputed by OPS-VERIFY from primitives; every factor carries the `OX-` evidence ids behind
   it, and the ledger chain is verified before anything else is believed.
5. **Long-running is wiring, not aspiration.** Each API read is a checkpointed stage;
   `make kill` exits 70 mid-acquisition and `make resume` finishes with the same `profile_id`, no
   duplicate evidence and no re-charge against the budget (verified by two tests).

## What this package deliberately does not do

* It does not connect to a real core banking system — internal APIs are mocked, as agreed.
* It does not decide anything, and there is no flag that makes it decide anything.
* It does not send email; it produces an approved-gated draft.
* It does not model policy. Change the policy and every conclusion here changes with it: that is the
  design, and `policy-agent/proposals/P-2026-013.json` is an example of a change that moved one of
  these cases.
