# 02 · Skills and capabilities

A **skill** is a unit of work an agent performs; a **capability** is a permission it exercises to do
so. Every skill below states, without exception:

* **Inputs** — the fields it consumes and where they come from
* **Tools** — the capabilities it calls (`config/tools.yaml`)
* **Outputs** — the fields it writes and the artefact they land in
* **Evidence** — the ledger record type and the ids it emits (see `04-evidence-writes.md`)
* **Fails when** — the conditions that stop it rather than produce a number
* **Acceptance** — the test that proves it works (`tests/test_ops_agents.py`)

Twenty-three skills across four agents. None of them decides anything.

---

# OPS-PROFILE · internal facts

Capabilities: `C1 internal.read`, `C2 policy.evaluate`, `C6 profile.publish`.
Prohibited for this agent: anything that writes to a system of record or reaches a customer.

## SK-P1 · Staged acquisition

| | |
|---|---|
| **Inputs** | `case_id`, `customer_id`, `as_of`, `window_days`, `RunState` (checkpoint), budget |
| **Tools** | `c360.customers`, `c360.related_parties`, `c360.ownership`, `tx.summary`, `tx.flows` (paginated), `counterparties.detail`, `risk.ratings`, `lists.jurisdictions`, `docs.documents`, `rfi.threads` |
| **Outputs** | in-memory bodies + `data_quality.api` (calls, p95 latency, retries, endpoints list) |
| **Evidence** | one `API_CALL` per request (endpoint, `request_hash`, `response_hash`, status, latency, attempt); pages charged to the page budget |
| **Fails when** | the call budget or page budget is exhausted → `BudgetExceeded`, run state `BUDGET_EXHAUSTED`, no partial numbers emitted; the process is killed → checkpoint replayed on resume |
| **Acceptance** | `test_api_failures_are_retried_and_recorded`, `test_budget_exhaustion_is_a_partial_not_a_fabrication` |

Each read is a **stage**: `c360`, `related-parties`, `ownership`, `tx-summary`, `tx-flows`,
`risk-rating`, `jurisdiction-lists`, `documents`, `rfi-thread`, `counterparty-<id>`. A stage present in
the checkpoint is replayed, never refetched and never re-charged.

## SK-P2 · Customer 360 and related parties

| | |
|---|---|
| **Inputs** | `/c360/customers/{customer_id}`, `/c360/related-parties/{customer_id}` |
| **Tools** | `c360.*` |
| **Outputs** | `customer_360.legal_name`, `entity_type`, `registration`, `sector`, `segments`, `channels`, `introducer`, `onboarding`, `employees`, `premises`, `turnover_band`, `prior_sar`, `related_parties[]` (party id, name, role, nationality, residency, PEP flag and basis, ownership %) |
| **Evidence** | `OX-` ids of the two API records, linked from every factor derived from them |
| **Fails when** | the master record has no `legal_name` (schema minimum length) — a nameless customer is a data incident, not a profile |
| **Acceptance** | `test_profile_matches_its_schema`, `test_geography_uses_the_policy_lists` |

## SK-P3 · Transaction behaviour

| | |
|---|---|
| **Inputs** | `/tx/summary/{customer_id}?window=`, `/tx/flows/{customer_id}` (all pages) |
| **Tools** | `tx.summary`, `tx.flows` |
| **Outputs** | `txn_count`, `total_in`, `total_out`, `avg_txn`, `median_txn`, `max_single`, `cash_ratio`, `round_amount_ratio`, `cross_border_ratio`, `new_counterparties`, `peer_percentile`, `declared_monthly_turnover`, `observed_monthly_mean`, `prior_window_monthly_mean`, `declared_vs_observed_pct`, `period_on_period_pct`, `monthly_cv`, `dormant_days_before_activity`, `top_counterparty_hhi`, `max_month_share`, `monthly_inflows[]` |
| **Evidence** | `OX-` ids for the summary call and **every** `tx/flows` page, rolled into `evidence_ids` of `F-BEH-01..03` |
| **Fails when** | a month is absent from the feed — the month is named in `data_quality.notes` and excluded from the mean rather than interpolated; the pages disagree with the summary total → the deviation is reported, not smoothed |
| **Acceptance** | `test_transaction_metrics_re_derive` (recomputes mean and deviation from the monthly series) |

Arithmetic is fixed by rule, not by judgement: HHI is the sum of squared counterparty shares,
`declared_vs_observed_pct = (observed_mean − declared) / declared × 100`, `monthly_cv` is the
coefficient of variation of monthly inflows.

## SK-P4 · Counterparty network and shell indicators

| | |
|---|---|
| **Inputs** | `top_counterparties` from the flow pages, `/counterparties/counterparties/{id}` |
| **Tools** | `counterparties.detail` (one checkpointed stage per counterparty) |
| **Outputs** | `counterparties.top[]` (id, name, country, amount, txn_count, first_seen, `is_new`, `entity_age_months`, `shell_flags[]`, `adverse_media`, `sanctions_screen`), `count_analysed`, `new_counterparties`, `new_high_value[]`, `shell_indicator_count`, `adverse_media_counterparties[]` |
| **Evidence** | the counterparty API records; `F-PROD-01` carries the concentration evidence, `F-CHAN-01` the new-counterparty evidence |
| **Fails when** | a counterparty id in the flow page does not resolve (404) — the row is kept with `name: null` and a `data_quality` note; the network is never silently shrunk |
| **Acceptance** | `test_every_factor_is_evidenced` |

Shell indicators are the four facts a reviewer can check: entity younger than the configured
threshold, no employees recorded, premises `NONE_FOUND`/`RESIDENTIAL`/`SHARED_SERVICED_OFFICE`, and a
shared address with other counterparties. `shell_indicator_count` counts *entities with ≥2
indicators*, not indicators.

## SK-P5 · Geographic exposure

| | |
|---|---|
| **Inputs** | incorporation country, country of operation, related-party nationalities, counterparty countries, `/lists/jurisdictions` |
| **Tools** | `lists.jurisdictions` (the **policy's** lists — never a hard-coded list in code) |
| **Outputs** | `nexus_countries`, `fatf_call_for_action`, `fatf_grey_list`, `sanctioned`, `secrecy`, `high_corruption`, `lists_as_of` |
| **Evidence** | the lists API record, attached to `F-GEO-02..05` |
| **Fails when** | the lists endpoint is unavailable — geography is emitted with `lists_as_of: null`… which the schema rejects, so the run stops: exposure cannot be asserted without a list
| **Acceptance** | `test_geography_uses_the_policy_lists` |

## SK-P6 · Ownership graph, scored not completed

| | |
|---|---|
| **Inputs** | `/c360/ownership/{customer_id}` (paths with percentages and chains, unresolved holders, direct holdings, nominee flag) |
| **Tools** | `c360.ownership` |
| **Outputs** | `paths[]`, `unresolved[]`, `direct_holdings[]`, `nominee_declared`, `layers`, `unresolved_pct`, `natural_persons` |
| **Evidence** | the ownership API record + the related-parties record; `F-CUST-04/05/07` cite both |
| **Fails when** | the direct holdings do not sum to 100 ± 0.01 → `sum_check: DISCREPANCY` and the gap is named; never renormalised |
| **Acceptance** | `test_ownership_gap_is_reported_not_filled` |

`unresolved_pct` is the share of the entity held through holders with no identified natural person —
computed from declared percentages only. A nominee without a declared beneficiary is 45%, not "the
director, probably".

## SK-P7 · Screening position

| | |
|---|---|
| **Inputs** | related parties, `/lists/jurisdictions` version, `screening.required_lists` from the policy pack, optional connector output |
| **Tools** | `policy.evaluate` (for the required list set), `lists.jurisdictions` |
| **Outputs** | `queries[]`, `lists_queried[]`, `lists_unavailable[]`, `required_lists[]`, `hits[]` (list, matched name, strength, basis, resolved), `screening_complete` |
| **Evidence** | the lists record; every hit is carried into the profile record and re-emitted by OPS-VERIFY
| **Fails when** | never fails outright — an unavailable list is a disclosure: the list is named, `screening_complete: false`, and `COMPLETE_SCREENING` becomes an obligation downstream |
| **Acceptance** | `test_unavailable_list_makes_screening_incomplete` |

The agent never decides whether a hit is a true match. `strength` is the provider's, `resolved` is a
human's.

## SK-P8 · Document coverage

| | |
|---|---|
| **Inputs** | `/docs/documents/{case_id}` (RDE elements with status, document, date, blocking flag), the ownership result |
| **Tools** | `docs.documents` |
| **Outputs** | `coverage.elements[]`, `applicable`, `satisfied`, `coverage_pct`, `blocking_rde_ids[]`, `missing_rde_ids[]` |
| **Evidence** | the documents API record is carried on every element-derived factor |
| **Fails when** | the documents endpoint is unavailable → the profile stops; "no documents" is not the same as "cannot see the documents" |
| **Acceptance** | `test_coverage_is_arithmetic` |

The agent may only make coverage **worse** with evidence: if the ownership endpoint shows an
unresolved holder, an RDE-03/04 element that the document store marks `SATISFIED` is downgraded to
`PARTIAL` with a note. It never upgrades an element.

## SK-P9 · Factor mapping

| | |
|---|---|
| **Inputs** | everything above; the factor catalogue from the policy pack (`data.risk.factors`) |
| **Tools** | `policy.evaluate` (catalogue) |
| **Outputs** | `factors{F-…: bool}` (all twenty), `factors_triggered[]`, `observations{F-…: {rule, value, evidence_ids, note}}` |
| **Evidence** | every observation carries ≥1 resolving `OX-` id; V-002 fails the run otherwise |
| **Fails when** | a factor id is not in the pack → `KeyError`, run stops. The agent may not invent a factor, may not drop one, and may not set one to false to lower a score |
| **Acceptance** | `test_all_twenty_factors_are_present_and_triggered_list_agrees`, `test_unknown_factor_id_is_a_hard_error` |

## SK-P10 · Profile delta and refresh

| | |
|---|---|
| **Inputs** | previous profile (from `out/profiles/` or the ledger), the new profile |
| **Tools** | none |
| **Outputs** | `delta[]` with `kind ∈ {FACTOR_FLIPPED, COVERAGE_MOVED, INTERNAL_RISK_MOVED}` and before/after values |
| **Evidence** | `PROFILE_DELTA` record keyed on (customer, previous id, canonical delta) — re-running the same comparison adds nothing |
| **Fails when** | the previous profile belongs to another customer → refused |
| **Acceptance** | `test_delta_reports_what_moved` |

---

# OPS-VERIFY · the adversarial reviewer

Capabilities: `C2 policy.evaluate`, `C3 verification.write`.
Prohibited for this agent: all writing capabilities, all customer contact, all data-gathering.

The thirteen checks are one battery, run in full on every bundle. They are grouped into five skills
below; the check id is the unit of evidence and of severity.

## SK-V1 · Evidence integrity and traceability · V-001, V-002

| | |
|---|---|
| **Inputs** | the ledger, every `evidence_ids` field in the bundle |
| **Tools** | `policy.evaluate` (not used), ledger read |
| **Outputs** | V-001 verdict + the head hash; V-002 verdict + the number of references resolved |
| **Evidence** | `VERIFICATION` record with `checks[]` and `critic_status` |
| **Fails when** | a `record_hash` does not re-derive from its record, a `prev_hash` does not match the previous record, a `signature` is malformed, a sequence number skips, or an `OX-` id does not exist |
| **Acceptance** | `test_broken_chain_is_caught`, `test_every_factor_is_evidenced` |

## SK-V2 · Arithmetic re-derivation · V-003

| | |
|---|---|
| **Inputs** | profile primitives (monthly inflows, holdings, counterparty amounts, document elements) |
| **Tools** | none — recomputation, not comparison |
| **Outputs** | V-003 verdict naming which figure disagreed and by how much |
| **Evidence** | the failing figure and the recomputed value in the check `detail` |
| **Fails when** | coverage %, unresolved %, HHI, deviation, or the factor map does not reproduce — tampering by one decimal is enough |
| **Acceptance** | `test_tampered_arithmetic_is_caught` |

## SK-V3 · Policy replay and envelope integrity · V-004, V-005

| | |
|---|---|
| **Inputs** | the pinned policy input, the recorded envelope, the live policy service |
| **Tools** | `policy.evaluate` |
| **Outputs** | V-004 (replay agreement on outcome, reasons, route, band, score), V-005 (closed enum, prohibitions pinned, obligations present) |
| **Evidence** | `VERIFICATION` record; a replay failure is `BLOCKING` |
| **Fails when** | a decision is present without its pinned input (not auditable), or re-evaluating produces a different outcome/band/score, or `agent_may_clear_sanctions_hit` is not `false` |
| **Acceptance** | `test_policy_replay_mismatch_is_caught` |

## SK-V4 · Cross-artefact consistency, gap disclosure, obligation coverage · V-006, V-007, V-008

| | |
|---|---|
| **Inputs** | profile, disposition, RFI |
| **Tools** | none |
| **Outputs** | three verdicts + details |
| **Evidence** | the mismatching fields named in `detail` |
| **Fails when** | the customer name, ownership figure or coverage percentage differs between artefacts; blocking gaps exist and are not disclosed; an obligation has no action, or an action cites an obligation the policy did not impose |
| **Acceptance** | `test_obligation_without_action_is_caught` |

## SK-V5 · Authority, approval, language, PII, injection, freshness · V-009 … V-013

| | |
|---|---|
| **Inputs** | approval records, the policy authority projection, all outbound and decision text, artefact timestamps |
| **Tools** | `policy.evaluate` (authority) |
| **Outputs** | five verdicts; `injection_attempt` mirrors V-012 |
| **Evidence** | `VERIFICATION`; each hit names the pattern and the artefact |
| **Fails when** | a gated action lacks `rfi.issue`, an approval requirement already names an approver, a grant has no TTL or names an agent, an approver equals the requester, prohibited language appears, PII appears in outbound text, instruction-like text appears in any artefact, or the profile is older than 30 days |
| **Acceptance** | `test_tipping_off_language_blocks`, `test_pii_in_outbound_text_blocks`, `test_injection_contamination_sets_the_flag`, `test_self_approval_is_caught`, `test_agent_named_approver_is_caught`, `test_stale_profile_fails_freshness` |

Verdict → severity → consequence:

| verdict | severity | consequence |
|---|---|---|
| FAIL | BLOCKING | `critic_status: FAIL`, blocking id listed, case `RETURNED_FOR_REWORK` |
| FAIL | MAJOR | `critic_status: FAIL` (a major failure is still a failure) |
| FAIL | MINOR | recorded, verdict unchanged |
| NOT_APPLICABLE | — | recorded with a reason; four checks are typically N/A before a decision exists |
| bundle without a profile | BLOCKING (V-000) | `critic_status: FAIL` — there is nothing to verify |

---

# OPS-OUTREACH · the customer conversation

Capabilities: `C4 rfi.draft`, `C5 rfi.issue` (request only), `C2 policy.evaluate`.
Prohibited: sending, deciding, scoring, promising outcomes.

## SK-O1 · Gap to question selection

| | |
|---|---|
| **Inputs** | obligations (from the policy or, pre-policy, from the profile's gaps), coverage elements |
| **Tools** | `policy.evaluate` |
| **Outputs** | `questions[]` with `rde_id`, `question`, `documents[]`, `obligation`, `closes_gap` |
| **Evidence** | part of the `RFI` draft record |
| **Fails when** | every open obligation is internal (`COMPLETE_SCREENING`, `HUMAN_EDD_APPROVAL`, `ESCALATE_MLRO`) → `OutreachRefused("no customer-answerable gaps")`. A customer is never asked to complete the bank's screening |
| **Acceptance** | `test_customer_questions_map_to_requirements`, `test_internal_only_case_produces_no_letter` |

## SK-O2 · Drafting and tone

| | |
|---|---|
| **Inputs** | questions, customer name, locale (`config.ops.outreach.locale`), templates |
| **Tools** | model role `outreach.draft` |
| **Outputs** | `subject`, `body`, `message_hash`, `language{locale, reading_level, tipping_off_check}` |
| **Evidence** | `RFI` record with the hash, not the body: the letter is reproducible from the hash and the template version |
| **Fails when** | the draft contains prohibited language or PII (V-010/V-011) — the draft is emitted and the verification fails it; the agent does not self-censor silently |
| **Acceptance** | `test_draft_contains_no_prohibited_language_or_pii`, `test_draft_is_ready_to_send_never_sent` |

## SK-O3 · Deadline and round budget

| | |
|---|---|
| **Inputs** | `as_of`, `rfi_max_rounds` from the policy pack, `config.ops.outreach.deadline_business_days`, the thread's completed rounds |
| **Tools** | none |
| **Outputs** | `round`, `max_rounds`, `due_date` (business days, weekends skipped), state |
| **Evidence** | `RFI` record includes round and deadline |
| **Fails when** | `rounds_completed` ≥ `max_rounds` → `OutreachRefused("rounds exhausted")`. A third letter on a two-round policy is a compliance finding |
| **Acceptance** | `test_round_beyond_policy_maximum_is_refused`, `test_due_date_is_ten_business_days` |

## SK-O4 · Approval request (never a send)

| | |
|---|---|
| **Inputs** | the draft, `config.ops.outreach.approval_roles`, the authority projection for `rfi.issue` |
| **Tools** | `policy.evaluate` (authority), `C5` (request) |
| **Outputs** | `approval{required: true, action: "rfi.issue", requested_by: "OPS-OUTREACH", approver_role, ttl_hours, approver: null, state: "PENDING"}` |
| **Evidence** | `RFI` (`DRAFTED`); on grant, a second `RFI` (`SENT`) carrying the approval |
| **Fails when** | `send()` is called with no approver, an agent id as approver, the requester as approver, or `ttl_hours <= 0` → `OutreachRefused`; the authority projection does not return `approval_required` → refused rather than assumed |
| **Acceptance** | `test_send_requires_a_named_human_with_a_ttl` |

## SK-O5 · Response assessment

| | |
|---|---|
| **Inputs** | the RFI, the customer response (`documents[]` with kind, rde ids, signed, dated, content flags), the profile |
| **Tools** | model role `outreach.assess` |
| **Outputs** | `outcome` (`SATISFIED`/`PARTIAL`/`INSUFFICIENT`), `satisfied_rde_ids`, `partial_rde_ids`, `missing_rde_ids`, `red_flags[]`, `rde_updates[]`, `rounds_used`, `next_action` |
| **Evidence** | `RFI` (`RESPONSE_ASSESSED`) |
| **Fails when** | a document is unsigned, undated, expired, for the wrong entity or illegible → the requirement it was meant to close is `PARTIAL`, with the flag named |
| **Acceptance** | `test_assessment_outcomes_and_next_actions` |

## SK-O6 · Follow-up and escalation

| | |
|---|---|
| **Inputs** | the assessment, rounds used vs allowed |
| **Tools** | none |
| **Outputs** | `next_action ∈ {REASSESS_AND_DECIDE, FOLLOW_UP, ESCALATE_ROUNDS_EXHAUSTED}`, `profile_must_be_refreshed` |
| **Evidence** | the assessment record |
| **Fails when** | rounds are exhausted and the gap remains open → `ESCALATE_ROUNDS_EXHAUSTED`; the agent may not invent a further round or fill the gap itself |
| **Acceptance** | `test_assessment_outcomes_and_next_actions` |

---

# OPS-DECIDE · the composer

Capabilities: `C2 policy.evaluate`, `C7 disposition.propose`, `C8 escalation.create` (request).
Prohibited: any decision-making capability, any customer contact, any policy authoring.

## SK-D1 · Policy input assembly

| | |
|---|---|
| **Inputs** | case (id, requested CDD level, jurisdiction profile), profile (factors, screening, ownership, coverage), verification (`critic_status`, `injection_attempt`), `input_hash` |
| **Tools** | `policy.evaluate` (factor catalogue, to reject undefined ids) |
| **Outputs** | the pinned policy input document, stored verbatim as `policy_input` for replay |
| **Evidence** | `DISPOSITION` record references the decision id; the pinned input is what makes V-004 possible |
| **Fails when** | a factor id is absent from the catalogue → `ValueError`; the verification report is missing → refused (a decision without a critic status is a gate the policy cannot apply) |
| **Acceptance** | `test_undeclared_factor_stops_the_run` |

## SK-D2 · Envelope projection

| | |
|---|---|
| **Inputs** | `data.agent.decision.result` |
| **Tools** | `policy.evaluate` |
| **Outputs** | `policy{decision_id, policy_version, outcome, reasons[], route, required_gates[], risk{score, band, weighted_score_band, cdd_level}, auto_final_allowed: false, agent_may_clear_sanctions_hit: false}` |
| **Evidence** | `DISPOSITION` record (policy outcome, recommendation, admissibility, human approval required) |
| **Fails when** | the policy service is unreachable or returns undefined → `PolicyUnavailable`, run stops. No envelope is ever reconstructed from memory |
| **Acceptance** | `test_policy_envelope_is_projected_verbatim` |

## SK-D3 · Recommendation selection and conservative downgrade

| | |
|---|---|
| **Inputs** | the envelope, the profile, `data.agent.disposition.result` for each candidate |
| **Tools** | `policy.evaluate` (dispose) |
| **Outputs** | `recommendation`, `recommendation_proposed`, `policy_admissibility{admissible, reason, allowed_recommendations[], downgraded_from, approval_reason, agent_may_finalise: false}` |
| **Evidence** | the admissibility block travels with the `DISPOSITION` record |
| **Fails when** | the preferred recommendation is not in the band's matrix → walk `REFER_MLRO → ESCALATE_EDD → HOLD_FOR_RFI → PROCEED_WITH_CONDITIONS → PROCEED`; if none is admissible → raise, never manufacture a disposition |
| **Acceptance** | `test_recommendation_is_admissible_for_the_band`, `test_inadmissible_recommendation_is_downgraded_not_forced` |

Selection rule (deterministic, in this order):

| envelope | preferred recommendation |
|---|---|
| `BLOCK` with `SANCTIONS_HIT_REQUIRES_ADJUDICATION` | `REFER_MLRO` |
| `BLOCK` or `REQUIRE_EDD` with band `HIGH`/`VERY_HIGH` | `ESCALATE_EDD` |
| `BLOCK` or `REQUIRE_RFI` with evidence gaps | `HOLD_FOR_RFI` |
| `PERMIT_WITH_CONDITIONS` | `PROCEED_WITH_CONDITIONS` |
| `PERMIT` | `PROCEED` |

## SK-D4 · Obligation to action plan

| | |
|---|---|
| **Inputs** | `obligations[]` from the envelope, SLA table in `config.ops.decision.action_due_business_days`, the RFI if one exists |
| **Tools** | none |
| **Outputs** | `actions[]` with `obligation`, `owner`, `action`, `authority`, `due`, `status` (`OPEN`/`AWAITING_CUSTOMER`/`BLOCKED_NO_RFI`), `because`, `rfi_id` |
| **Evidence** | recorded in the `DISPOSITION` record via `content_hash` |
| **Fails when** | an obligation has no mapping → the action is emitted as `UNASSIGNED`/`NEEDS_POLICY_MAPPING` rather than dropped, and the run records it; obligations are never added or removed by the agent |
| **Acceptance** | `test_actions_cover_obligations_exactly`, `test_customer_obligation_without_rfi_is_blocked_and_says_why` |

## SK-D5 · Approval requirement and state

| | |
|---|---|
| **Inputs** | envelope route, verification status, blocking gates |
| **Tools** | `policy.evaluate` |
| **Outputs** | `approval{required: true, approver_role, route, approver: null, approved_at: null, state: "PENDING", blocked_by[], may_be_approved_while_blocked}`, `state ∈ {PENDING_APPROVAL, RETURNED_FOR_REWORK}`, `finalised: false` |
| **Evidence** | `DISPOSITION` record |
| **Fails when** | never: there is no path in which `required` is false or `approver` is non-null |
| **Acceptance** | `test_disposition_matches_schema_and_is_never_final`, `test_critic_failure_returns_the_case_for_rework` |

## SK-D6 · Narrative

| | |
|---|---|
| **Inputs** | envelope, admissibility, obligations, verification, approval |
| **Tools** | model role `decision.narrative` |
| **Outputs** | `narrative` naming outcome, score, band, floor applied, reasons, coverage and blocking gaps, obligations, verification result, recommendation, approver role, and the statement that automatic finalisation is unavailable |
| **Evidence** | `content_hash` in the `DISPOSITION` record |
| **Fails when** | the narrative drops a policy reason, adds a reason or obligation the envelope did not contain, or claims the case is closed → OPS-VERIFY V-010/V-006 fails the bundle |
| **Acceptance** | `test_narrative_quotes_the_policy_and_refuses_finalisation`, `test_narrative_introduces_no_reason_the_policy_did_not_give` |

## SK-D7 · Re-decision diff

| | |
|---|---|
| **Inputs** | the previous disposition, the new one |
| **Tools** | none |
| **Outputs** | `diff[]` with `kind ∈ {RECOMMENDATION_MOVED, OUTCOME_MOVED, BAND_MOVED, CRITIC_MOVED, REASON_APPEARED, REASON_DISAPPEARED}` |
| **Evidence** | `EVENT` record `REDECISION_DIFF` |
| **Fails when** | the same evidence produces a different recommendation — the diff makes it visible instead of letting it pass quietly |
| **Acceptance** | `test_diff_reports_what_changed` |
