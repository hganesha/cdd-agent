# 00 · Agent definition — the OPS case-operation team

**Package:** `ops-case-operations` · v1.0.0
**Agents:** `OPS-PROFILE`, `OPS-VERIFY`, `OPS-OUTREACH`, `OPS-DECIDE`
**Policy dependency:** `AXIOM-POLICY` (`cdd-policy-2026.4`, OPA/Rego)
**Reference implementation:** `src/opsagents/` · **Contract tests:** `tests/test_ops_agent.py` (53)

---

## 1. What this system is

Four agents run one financial-crime case. They are separated not by prompt but by *blast radius*:
the agent that reads the bank's data cannot talk to the customer; the agent that writes to the
customer cannot score; the agent that composes a recommendation cannot decide; and the agent that
checks all three has no production authority at all.

```
                       ┌──────────────────────────────────────────────┐
                       │              AXIOM-POLICY (OPA)              │
                       │  risk · band · ubo · screening · coverage    │
                       │  gates · outcome · obligations · authority   │
                       └───────▲───────────────▲──────────────▲───────┘
                       policy  │        policy │       policy │
                            ┌──┴───┐       ┌───┴────┐     ┌────┴─────┐
   internal APIs ─────────►│OPS-  │──────►│OPS-    │────►│OPS-      │
   (mock/api/)             │PROFILE│      │VERIFY  │     │DECIDE    │──► recommendation
                            └──┬───┘       └───▲────┘     └────┬─────┘    + approval requirement
                               │ profile       │ verification  │ obligations
                               ▼               │               ▼
                          ┌────────────┐       │        ┌─────────────┐
                          │OPS-OUTREACH│───────┘        │   HUMAN     │
                          └─────┬──────┘  drafts        │ approver    │
                                │                        └─────────────┘
                                ▼
                        customer (never from an agent directly)

      every arrow writes to ONE append-only, hash-chained ledger: out/ops_evidence.jsonl
```

## 2. The four agents

### 2.1 `OPS-PROFILE` — internal facts

**Purpose.** Turn the bank's own records into a single, evidenced, policy-shaped profile.

**Inputs.** `case_id`, `customer_id`, `as_of`, `window_days` (default 90), an optional external
`evidence_pack` from the search agent, optional screening output.

**Produces.** `customer-profile.json` (`schemas/customer-profile.schema.json`): customer 360 and
related parties; transaction behaviour with re-derivable arithmetic; counterparty network with shell
indicators; geographic nexus against the policy's own lists; internal rating and expected-vs-observed;
ownership paths with an explicit unresolved share; screening position including unavailable lists;
document coverage against the RDE set; all twenty policy factors with the rule, the value and the
evidence behind each.

**Never.** Sets a rating, decides anything, drafts or sends customer contact, invents a factor id,
treats an unavailable list as a pass, or completes an ownership graph by inference.

### 2.2 `OPS-VERIFY` — the adversarial reviewer

**Purpose.** Decide whether the other agents' output can be believed, and publish exactly one field
the rest of the stack acts on: `critic_status`.

**Inputs.** The bundle under review: case, profile, evidence pack, pinned policy input, policy
decision, disposition, RFI, approval record, `as_of`.

**Produces.** `verification-report.json`: thirteen checks with verdicts, severities, blocking flags
and a detail string naming what was examined; `critic_status`; `injection_attempt`;
`blocking_check_ids`.

**Never.** Gathers facts, edits an artefact to make it pass, downgrades a severity, clears a hit, or
lets a previous PASS stand in for this run.

### 2.3 `OPS-OUTREACH` — the customer conversation

**Purpose.** Ask the customer only what the customer can answer, in language that cannot tip them off,
and judge what comes back.

**Inputs.** Case, profile, obligations (policy-derived or pre-policy), the RFI thread (rounds used,
prior outcomes), `as_of`.

**Produces.** `rfi.json`: one draft per round with per-question requirement mapping, documents
requested, internal actions separated out, a business-day deadline, `message_hash`, and an approval
requirement with `approver: null`. On a reply: an assessment (`SATISFIED` / `PARTIAL` /
`INSUFFICIENT`) with a closed-set next action.

**Never.** Sends, exceeds the policy's `rfi_max_rounds`, asks for an internal obligation, mentions
screening/policy/monitoring, offers reassurance, or includes third-party PII.

### 2.4 `OPS-DECIDE` — the composer

**Purpose.** Compose the policy envelope, the admissibility of a recommendation, the action plan and
the approval requirement into a disposition a human can act on.

**Inputs.** Case, profile, verification report, optional RFI, pinned policy input.

**Produces.** `disposition.json`: the policy projection verbatim; the admissibility result (with
`downgraded_from` when the preferred recommendation was not admissible); obligations turned into
owned, dated actions; the approval requirement with role and `approver: null`; state
(`PENDING_APPROVAL` / `RETURNED_FOR_REWORK`); a narrative that quotes the policy; `finalised: false`.

**Never.** Decides, finalises, writes its own reasons/gates/obligations/thresholds, presents a
recommendation outside the band's matrix, or clears a sanctions hit.

## 3. Handoff contracts

| From → To | Payload | Guarantee |
|---|---|---|
| PROFILE → VERIFY | `customer-profile.json` | every factor names resolvable `OX-` evidence; all twenty factors present |
| PROFILE → DECIDE | `factors`, `screening`, `ownership`, `coverage` | values are copied into the policy input unchanged; an undefined factor raises |
| VERIFY → DECIDE | `critic_status`, `blocking_check_ids`, `verification_id` | forwarded to the policy service, never softened |
| DECIDE → OUTREACH | `obligations[]` with `because` codes | only obligations the policy imposed, each mapped to an action |
| OUTREACH → DECIDE | `rfi_id`, `state`, `round` | a draft can never be `SENT` without a human approval grant |
| DECIDE → human | recommendation + approval requirement | `approver: null`, `finalised: false`, role named |

A handoff is a JSON document validated against a schema in `schemas/`, written to a deterministic
path in `out/`, and recorded in the ledger with an `OX-` id. Nothing is passed by conversation.

## 4. Capabilities

A *capability* is a permission to do something with a side effect, and each is granted to exactly
one agent. Capabilities are declared in `config/tools.yaml` (`grants:`) and are enforced twice: by
the gateway, and by OPS-VERIFY's V-009 check that reads `data.agent.authority.result` for every action
an artefact claims to have used.

| id | capability | granted to | side effect | evidence write |
|---|---|---|---|---|
| C1 | `internal.read` — c360, tx, counterparties, ratings, lists, docs, rfi thread | PROFILE | none (read) | `API_CALL` per call, per page |
| C2 | `policy.evaluate` — decide / dispose / authority | all four (read-only) | none | `EVENT` on evaluation; the envelope travels with the artefact |
| C3 | `verification.write` — publish a critic verdict | VERIFY | changes the policy outcome | `VERIFICATION` |
| C4 | `rfi.draft` — compose a customer letter | OUTREACH | none until approved | `RFI` (`DRAFTED`) |
| C5 | `rfi.issue` (gated) — send | **no agent**; requests only | customer contact | `RFI` (`SENT`) with the approval grant |
| C6 | `profile.publish` — publish a profile | PROFILE | downstream decisions | `PROFILE` |
| C7 | `disposition.propose` — publish a recommendation | DECIDE | human workload | `DISPOSITION` |
| C8 | `escalation.create` (gated) — raise to MLRO | DECIDE requests | case routing | `EVENT` |

Prohibited for every agent, at every risk level, in every circumstance: `case.finalise`,
`customer.approve`, `sanctions.clear`, `policy.activate`, `evidence.rewrite`.

## 5. Human-in-the-loop points

1. **Approval of the disposition** — mandatory for every case. The policy returns
   `human_approval_required: true` and `agent_may_finalise: false` for every band; the route names
   *who* (MLRO, EDD unit head, compliance officer) and *why* (`GATES_FIRED`, `CRITIC_FAILED`,
   `HUMAN_APPROVAL_ALWAYS_REQUIRED`).
2. **Approval of customer contact** — `rfi.issue` is gated. `OPS-OUTREACH.send()` requires a named
   human, an approver role, and a TTL > 0, and refuses when the approver is the requester or an agent
   id.
3. **Sanctions adjudication** — a true or possible hit is never cleared by software; the policy
   returns `agent_may_clear_sanctions_hit: false` and the obligation `ESCALATE_MLRO`.
4. **Policy change** — the agents consume policy, they never author it. Proposals live in
   `policy-agent/proposals/` with `permitted_by_agent: false`.

## 6. Non-goals

* Not a case-management system: no queues, no SLAs against humans, no reporting packs.
* Not a document ingestion pipeline — that is the CDD agent's job; this package consumes its output.
* Not a screening vendor integration: the list providers are represented by `lists_unavailable` and
  hit records.
* Not an autonomous decision engine. If a deployment removes the human approval gate, this package
  no longer satisfies the policy contract it was written against, and OPS-VERIFY will fail every
  bundle rather than proceed.

## 7. Deployment shape

| Concern | Here | In production |
|---|---|---|
| internal systems | `mock/api/` fixtures | the bank's APIs, same shapes |
| policy engine | `opa eval` subprocess on a local bundle | OPA as a sidecar/daemon, same bundle, pinned revision |
| evidence | JSONL file, hash-chained, `kms-stub:` signatures | append-only store (WORM bucket) with KMS signing |
| checkpoints | JSON files under `out/state/` | object storage, same atomic-write discipline |
| models | deterministic code; narrative roles per `config/model-routing.yaml` | same, with pinned model ids |

The rules that matter are in the code and the policy pack, not in the deployment: swapping the mock
for real APIs changes no factor, no threshold and no conclusion.
