# 02 · Skills

Nine skills. In this agent the word "skill" means a **capability of the policy service**, and every
one of them is Rego or Python — none is a model call. Inputs, outputs and evidence writes are stated
exactly; the acceptance column names the Rego or Python test that fails if the skill regresses.

---

## SK-P1 · Risk scoring and banding

| | |
|---|---|
| Kind | Rego `cdd.risk` |
| Purpose | Turn a set of triggered risk factors into a 0–100 score, a band, and a CDD level — with every point attributable |

**Inputs** — `input.factors: {factor_id: bool}` (20 permitted ids), `data.config.risk.{weights,bands,cdd_level,factors,band_floors}`.

**Rules**
* Unknown factor id → `valid_input: false` → the envelope becomes `INVALID_INPUT` / `BLOCK`. An agent
  cannot invent a mitigating factor.
* Dimension score = sum of the points of the factors triggered in that dimension, capped at 100;
  weighted score = Σ(weight × dimension); weights `customer .35 / product .20 / geography .25 /
  channel .10 / behaviour .10`.
* Bands: `LOW 0–39`, `MEDIUM 40–59`, `HIGH 60–79`, `VERY_HIGH 80–100`.
* **Band floors**: 8 conditions can raise the band regardless of the weighted score (e.g. `F-CUST-03`
  sanctions proximity → `VERY_HIGH`, `F-GEO-01` FATF call-for-action nexus → `VERY_HIGH`,
  `F-CUST-01` PEP exposure → `HIGH`). Nothing lowers a band. `applied_overrides` records each.
* `cdd_level` maps band → `SDD | CDD | EDD`.

**Outputs** — `{score, band, weighted_score_band, cdd_level, dimension_scores, triggered_factors, applied_overrides, trace[], valid_input}`.

**Evidence writes** — embedded in the decision record: the score, the band, the weighted band and the
override list travel with the `decision_id`, so a reviewer can see *why* 37.95 became `HIGH`.

**Failure** — undefined result is impossible for a well-typed input; an unknown factor is a hard
`INVALID_INPUT`, never a default.

**Acceptance** — `decision_test.rego` band/floor/unknown-factor cases; Python
`test_python_and_opa_risk_engines_agree` (agreement to ±0.01 with the CDD agent's independent Python
model across 8 factor combinations); `test_unknown_factor_is_rejected`.

---

## SK-P2 · Beneficial ownership and the control prong

| | |
|---|---|
| Kind | Rego `cdd.ubo` |
| Purpose | Decide who the beneficial owners are, whether a chain reaches a natural person, and whether an unresolved share blocks the conclusion |

**Inputs** — `input.ownership: {paths: [{person, pcts[]}], unresolved: [{holder, pct}], direct_holdings: [{holder, pct}], nominee_declared: bool}`, `data.config.thresholds.{ubo_pct: 25, max_lookthrough_depth: 4}`.

**Rules**
* A natural person is a beneficial owner when effective ownership ≥ 25 % — **inclusive**: exactly 25 %
  qualifies (`test_ubo_threshold_is_inclusive_at_25_pct`).
* Effective percentage = product along each path (multiplicative look-through), summed across paths,
  to depth 4.
* **Ownership prong** OR **control prong**: where no natural person reaches 25 %, a control-basis
  determination must be recorded; `control_prong_missing` when control is claimed but no basis is
  given.
* `unresolved_pct` = Σ unresolved holder percentages. `blocks_conclusion` when ownership cannot be
  traced to a natural person (§5.4) **or** the control prong is missing.
* A nominee without an underlying principal declaration blocks before onboarding (§5.3).

**Outputs** — `{beneficial_owners[], effective_pct{}, unresolved_pct, max_depth_reached, control_prong_missing, blocks_conclusion, findings[]}`.

**Evidence writes** — in the decision record under `ubo`; the `beneficial_owners` list is what the
screening skill consumes, so an ownership outcome that changes the owner set changes screening too.

**Failure** — a cycle in the ownership graph is detected and reported as unresolved, never looped
through.

**Acceptance** — `ubo_22pct_single` (below threshold, control prong required), `ubo_24pct_two_paths`
(two 24 % paths do not sum into a 25 % owner), `nominee_unresolved` (45 % unresolved → `BLOCK`).

---

## SK-P3 · Screening completeness and sanctions prohibition

| | |
|---|---|
| Kind | Rego `cdd.screening` |
| Purpose | Establish whether screening is *complete*, whether a hit requires adjudication, and whether an agent could ever clear one |

**Inputs** — `input.screening: {queries: [{list}], hits: [{list, strength, matched_name}], lists_unavailable: []}`, `data.config.screening.{required_lists: [OFAC_SDN, UN_CONSOLIDATED, EU_CFSP, UK_OFSI, PEP_GLOBAL], blocking_strengths: [TRUE, STRONG]}`.

**Rules**
* `screening_complete` only when **all five** required lists were queried and none is unavailable
  (SOP §6.3: an unavailable list is not a clearance).
* `requires_adjudication` when a hit's strength ∈ `{TRUE, STRONG}` — the case may not proceed.
* `pep_exposure` when a PEP hit is present, driving the `PEP_EXPOSURE` reason and the EDD route.
* `agent_may_clear_sanctions_hit: false` is a **`const`** in `schemas/decision.schema.json`: a decision
  document that claims otherwise is invalid, and `sanctions.clear` is in `prohibited_actions`.
* Lookalike hits (`WEAK`/`FALSE`) do not block but remain in the record.

**Outputs** — `{screening_complete, lists_queried, lists_missing, requires_adjudication, pep_exposure, blocking_hits[]}`.

**Evidence writes** — in the decision record under `screening`; required lists and hits are enumerated
so an auditor can see which lists ran, not just that "screening passed".

**Acceptance** — `sanctions_true_hit` → `BLOCK` + `ESCALATE_MLRO`; `sanctions_lookalike` → `PERMIT`;
`test_agent_cannot_clear_a_sanctions_hit`; `test_screening_fixture_derives_queries_from_required_lists`.

---

## SK-P4 · Evidence coverage sufficiency

| | |
|---|---|
| Kind | Rego `cdd.coverage` |
| Purpose | Decide whether the evidence base is good enough for the CDD level being claimed |

**Inputs** — `input.coverage.elements: [{rde_id, status: SATISFIED|PARTIAL|MISSING|NOT_APPLICABLE}]`, `input.case.cdd_level_requested`, `data.config.thresholds.min_coverage_pct {SDD 90, CDD 95, EDD 100}`.

**Rules**
* `coverage_pct` = satisfied / applicable (×100, to one decimal). `NOT_APPLICABLE` elements leave
  both numerator and denominator.
* Below the minimum for the level → `COVERAGE_BELOW_MINIMUM` and `blocks_decision`.
* Elements marked `decision_blocking` that are not satisfied → `DECISION_BLOCKING_GAPS` with their
  `rde_id`s listed.

**Outputs** — `{coverage_pct, min_coverage_pct, satisfied[], missing[], decision_blocking[], blocking_rde_ids[], below_minimum, blocks_decision}`.

**Evidence writes** — the deciding record carries `coverage_pct`, the minimum applied and the
blocking `rde_id`s (e.g. `16.7 % / 100 % / [RDE-07, RDE-08]`).

**Acceptance** — `clean_corporate` and `clean_individual` clear their level; `test_coverage_below_minimum_blocks`;
the reference decisive case blocks at 16.7 %.

---

## SK-P5 · Gate routing and escalation

| | |
|---|---|
| Kind | Rego `cdd.gates` |
| Purpose | Decide which human gates a case must pass and where it routes — never whether it may be finalised automatically |

**Inputs** — the composed facts (`residual_high_or_above`, `unresolved_ubo`, `blocking_hit`,
`decision_blocking_gaps`, `coverage_below_minimum`, `screening_incomplete`, `critic_fail`,
`injection_attempt`), `data.config.gates.force` (8 conditions), `auto_final_allowed: false`.

**Rules**
* `required_gates` = forced conditions that are true, named by condition id — never by a queue
  number, so the gate a case must clear is legible in the decision itself:
  `RESIDUAL_HIGH_OR_ABOVE`, `UNRESOLVED_UBO`, `SANCTIONS_BLOCKING_HIT`, `PEP_EXPOSURE`,
  `COVERAGE_BELOW_MINIMUM`, `DECISION_BLOCKING_GAPS`, `SCREENING_INCOMPLETE`, `CRITIC_FAIL`,
  `INJECTION_ATTEMPT` (nine conditions; eight are in `gates.force`, `PEP_EXPOSURE` is recorded
  without forcing a separate gate).
* Route precedence: `MLRO` (blocking hit) > `EDD_UNIT` (EDD level, unresolved UBO, high residual risk)
  > `COMPLIANCE_OFFICER` (coverage or screening gaps) > `CASE_ANALYST`.
* `auto_final_allowed: false` is a `const` in the manifest and in the decision schema — no
  configuration can enable straight-through finalisation.
* A `CRITIC_FAIL` or `INJECTION_ATTEMPT` flag forces its gate regardless of every other outcome: a
  case whose input carries evidence of an injection attempt is reviewed by a human even if it would
  otherwise `PERMIT`.

**Outputs** — `{required_gates[], route, auto_final_allowed, forced_by}`.

**Evidence writes** — `gates` block in the decision record; the route is what a work-management system
acts on, so it is derived, never assigned.

**Acceptance** — `test_gate_route_precedence` (MLRO beats EDD_UNIT); the reference `CASE-4831`
routing to `EDD_UNIT` with six required gates including `INJECTION_ATTEMPT`; and the
`auto_final_allowed: false` pin in the decision schema.

---

## SK-P6 · Jurisdiction overlay

| | |
|---|---|
| Kind | Rego `jurisdiction.overlay` |
| Purpose | Apply jurisdiction-specific requirements on top of the base pack without forking it |

**Inputs** — `input.case.jurisdiction_profile` (e.g. `US_FED_FINCEN_CDD`, `EU_AMLD`),
`data.config` base thresholds.

**Rules** — an overlay may **add** obligations (e.g. `require_source_of_wealth`) and may tighten a
threshold; it may never relax a base rule. Overlay additions are listed in
`jurisdiction.overlay_applied` so the decision shows which layer added what.

**Outputs** — `{overlay_applied[], require_source_of_wealth, tightened_thresholds{}}`.

**Evidence writes** — `jurisdiction` block in the decision record.

**Acceptance** — `eu_profile_sow` → `PERMIT_WITH_CONDITIONS` with `OBTAIN_SOURCE_OF_WEALTH`.

---

## SK-P7 · Decision composition

| | |
|---|---|
| Kind | Rego `agent.decision` — the single entrypoint |
| Purpose | Compose the sub-decisions into one envelope with a closed-enum outcome and a stable id |

**Inputs** — the case payload; all of the above.

**Rules**
* `outcome` ∈ `{PERMIT, PERMIT_WITH_CONDITIONS, REQUIRE_RFI, REQUIRE_EDD, BLOCK, INVALID_INPUT}`,
  evaluated by precedence: invalid → block conditions (very high risk, blocking sanctions, unresolved
  UBO) → EDD → RFI (coverage blocks) → conditions (any obligation) → permit.
* `reasons[]` and `obligations[]` are derived from the same booleans, so a reason can never appear
  without its consequence.
* `decision_id = <case_id>:<policy_version>:sha256(json(case, factors, ownership, screening, coverage))`
  — identical input + identical policy ⇒ identical id. Idempotent by construction.
* `auto_final_allowed: false` is exported by the envelope itself.

**Outputs** — the full envelope (§00 §3), validated against `schemas/decision.schema.json`.

**Evidence writes** — `DecisionLedger.decision()` writes a `PZ-nnnnnn` record: input hash, decision,
policy version, bundle revision, elapsed ms, chain hash.

**Acceptance** — `TestPolicyAgent` decision tests: schema validation of the canonical envelope,
determinism (same input → same `decision_id`), the `agent_may_clear_sanctions_hit: const false` pin,
and the corpus baseline of 10 cases.

---

## SK-P8 · Action authority

| | |
|---|---|
| Kind | Rego `agent.authority` |
| Purpose | Answer "may this agent perform this action?" with three-valued capability and no negotiation |

**Inputs** — `{action: string, actor?}`; the tables in `authority.rego`.

**Rules**
* **11 prohibited**: `evidence.delete`, `evidence.mutate`, `case.close`, `case.approve`,
  `risk.override`, `sanctions.clear`, `report.file`, `account.freeze`, `network.arbitrary`,
  `policy.activate`, `policy.self_modify` → `denied`, unconditionally.
* **5 gated**: `rfi.issue` (CASE_ANALYST, 24 h), `escalation.create` (COMPLIANCE_OFFICER, 8 h),
  `risk.recommend` (CASE_ANALYST, 72 h), `policy.propose_change` (POLICY_OWNER, 168 h),
  `bundle.stage` (PLATFORM_SECURITY, 24 h) → `approval_required` with approver and TTL.
* **9 allowed**: `search.query`, `search.fetch`, `document.read`, `policy.evaluate`,
  `evidence.append`, `case.read`, `report.compose`, `bundle.verify`, … 
* **Unknown action → `denied`** (default-deny, `test_unknown_action_is_denied`).
* A self-approval request (same `actor` as the requesting agent) fails the approval precondition.

**Outputs** — `{action, decision: allowed|approval_required|denied, approver?, ttl_hours?, reason}`.

**Evidence writes** — `DecisionLedger.authority()` records every query, including denials: a pattern
of denied attempts is a security signal, not noise.

**Acceptance** — `authority_test.rego` (deny for all prohibited, gate for all gated, allow for the
allowed set, deny for unknown) + `test_authority_denies_and_gates` in Python.

---

## SK-P9 · Bundle lifecycle and policy authoring

| | |
|---|---|
| Kind | Python `axiom.bundle`, `axiom.author` (with `SYS-AUTHOR` proposal assistance) |
| Purpose | Make a policy change a reviewable, reproducible, reversible artefact |

**Inputs** — `policy/**` + `data.json`; a SOP clause (authoring); an approver name and ticket
(activation).

**Rules**
* `build` → deterministic tarball + manifest (`revision`, `policy_version`, `sha256`, `files{}`
  per-file hashes, `entrypoints`, `opa_version`). Two builds of the same tree give the same hash.
* `verify` → recompute every file hash and the archive hash; a tampered member fails verification and
  cannot be activated.
* `activate` → requires `approver` + `ticket`, writes an authority-evidence record, marks the previous
  revision `superseded`. No code path activates without a human.
* `author` → clause parse → `_implemented()` guard → sandbox patch → compile + full suite →
  impact over the 10-case corpus → materiality → `status: DRAFT`. A failing candidate exits 1 and is
  still written, with its failure recorded.

**Outputs** — `out/bundles/<rev>.tar.gz`, `<rev>.manifest.json`, `ACTIVE` pointer;
`proposals/P-….json` (DRAFT), `proposals/candidates/*.rego`.

**Evidence writes** — `PZ` records for build/verify/activate/author, with the approver and ticket on
activation.

**Acceptance** — `test_bundle_build_and_verify`, `test_bundle_tamper_is_detected`,
`test_activation_requires_a_named_approval`, `test_author_refuses_a_precondition_mismatch`,
`test_author_impact_analysis_changes_the_right_cases`.
