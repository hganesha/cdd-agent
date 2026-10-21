# 03 · I/O contracts

Five artefacts cross agent boundaries. Each has a JSON Schema in `schemas/`, a deterministic file
path under `out/`, a canonical form used for hashing, and a rule about who may write it.

| artefact | schema | written by | path | read by |
|---|---|---|---|---|
| request | `ops-request.schema.json` | orchestrator / human queue | — (in memory) | all four |
| profile | `customer-profile.schema.json` | OPS-PROFILE | `out/profiles/<CASE>.json` | OPS-VERIFY, OPS-DECIDE, OPS-OUTREACH |
| verification report | `verification-report.schema.json` | OPS-VERIFY | returned + ledger; `out/verifications/<CASE>-<stage>.json` when persisted | OPS-DECIDE, humans |
| RFI | `rfi.schema.json` | OPS-OUTREACH | `out/rfi/<RFI-ID>.json` | OPS-VERIFY, OPS-DECIDE |
| disposition | `disposition.schema.json` | OPS-DECIDE | `out/dispositions/<CASE>.json` | humans, approval queue |
| evidence record | `ops-evidence-record.schema.json` | every agent (append-only) | `out/ops_evidence.jsonl` | OPS-VERIFY, auditors |
| run state | `ops-run-state.schema.json` | every agent | `out/state/<CASE>-<AGENT>.json` | the same agent on resume |

## 1. Conventions that apply to all five

* **Timestamps** are ISO-8601 UTC with milliseconds and a `Z` suffix (`2026-10-21T00:00:00Z`).
* **Dates** (deadlines, document dates) are `YYYY-MM-DD`, no time, no zone.
* **Money** is a JSON number in the case currency (declared once in the case record), never a string,
  never formatted with separators.
* **Countries** are ISO-3166 alpha-2. **Currencies** are ISO-4217.
* **Ids** are opaque strings with a fixed prefix: `CASE-`, `CUST-`, `PRF-`, `VER-`, `RFI-`, `DSP-`,
  `OX-`, `RUN-`, `RDE-`, `F-`, `CP-`, `PARTY-`.
* **Null vs absent**: a field that could not be determined is present and `null`, with the reason in
  `data_quality.notes` or the check `detail`. Absent means "not part of this artefact".
* **Canonical JSON** for hashing is `json.dumps(obj, sort_keys=True, separators=(",", ":"))`
  UTF-8-encoded, hashed with SHA-256 and prefixed `sha256:`.

## 2. Request

```json
{
  "case_id": "CASE-4831",
  "customer_id": "CUST-1001",
  "customer_name": "Meridian Trade Holdings Ltd",
  "as_of": "2026-10-21T00:00:00Z",
  "window_days": 90,
  "cdd_level_requested": "EDD",
  "jurisdiction_profile": "EU_AMLD",
  "prior_rfi_rounds": 1,
  "evidence_pack": { "...": "optional output of WEBIS-SEARCH" },
  "screening": { "...": "optional connector output" }
}
```

`evidence_pack` being absent is legal and meaningful: the profile then says it has no external
adverse-media evidence. `screening` being absent means the profiler derives the screening position
from the party list and the required-list set, and discloses whatever is missing.

## 3. Profile (`customer-profile.schema.json`)

Required top-level keys, in schema order: `profile_id`, `case_id`, `customer_id`, `as_of`,
`window_days`, `agent`, `customer_360`, `transactions`, `counterparties`, `geography`,
`internal_risk`, `ownership`, `screening`, `coverage`, `factors`, `factors_triggered`,
`observations`, `outreach`, `data_quality`, `evidence_id`. `additionalProperties: false` — a new field
is a schema change, reviewed like any other.

Cross-field invariants the schema cannot express, enforced by tests and V-003:

| invariant | statement |
|---|---|
| I-P1 | `factors` has exactly the twenty ids in the policy catalogue, all booleans |
| I-P2 | `factors_triggered == sorted(k for k, v in factors.items() if v)` |
| I-P3 | every `observations[F-…].evidence_ids` is non-empty and every id resolves in the ledger |
| I-P4 | `coverage.applicable == |elements where status != NOT_APPLICABLE|` |
| I-P5 | `coverage.coverage_pct == round(satisfied / applicable × 100, 1)` |
| I-P6 | `blocking_rde_ids == [e.rde_id for e in applicable if e.blocks_decision and e.status != SATISFIED]` |
| I-P7 | `declared_vs_observed_pct == round((observed_monthly_mean − declared) / declared × 100, 1)` |
| I-P8 | `screening_complete == (lists_unavailable == [] and set(lists_queried) ⊇ set(required_lists))` |
| I-P9 | `profile_id` is a pure function of the inputs: same request and same bodies → same id |

## 4. Verification report (`verification-report.schema.json`)

* `checks` has `minItems: 13`; a report with fewer is invalid, and a report with no profile in the
  bundle carries a synthetic blocking `V-000`.
* `critic_status` is derived, never set by hand:
  `FAIL` if any BLOCKING or MAJOR check failed, else `PASS`.
* `injection_attempt` is exactly `checks[V-012].verdict == "FAIL"`.
* `subject_agent` lists the agents whose artefacts were examined, so a critic verdict is attributable.
* The report is **reproducible**: `verification_id = VER-<hash8>` over the canonical checks array. Two
  runs on the same bundle produce the same id — which is why the ledger's idempotency key includes it.

## 5. RFI (`rfi.schema.json`)

* `state` moves `READY_TO_SEND → PENDING_APPROVAL → SENT → RESPONSE_ASSESSED`, and may end at
  `REFUSED_ROUNDS_EXHAUSTED` or `CLOSED`. `READY_TO_SEND` is the furthest an agent can take it.
* `approval.approver` is `null` in every draft. `approval.approved_at` is `null` until a human acts.
* `message_hash` is the SHA-256 of the body. The body is not written to the ledger; the letter is
  reproducible from the hash plus the pinned template version.
* `questions[].rde_id` must appear in `covers_rde_ids`; every question names at least one document.
* `internal_actions[]` must not be empty when the obligations included an internal obligation, and no
  internal obligation may appear in `body` (checked by V-010's phrase list and by test).

## 6. Disposition (`disposition.schema.json`)

Schema-enforced invariants:

| field | constraint | why |
|---|---|---|
| `finalised` | `const false` | a disposition is a proposal |
| `policy.auto_final_allowed` | `const false` | the policy itself forbids automation |
| `policy.agent_may_clear_sanctions_hit` | `const false` | adjudication is a human act |
| `policy_admissibility.agent_may_finalise` | `const false` | from `data.agent.disposition.result` |
| `approval.required` | `const true` | every case needs a human, every band |
| `approval.approver` | `["string", "null"]`, null in practice | a name here before a human acted is a forgery |
| `recommendation` | closed enum of five | the policy's matrix vocabulary |
| `state` | closed enum | `RETURNED_FOR_REWORK` / `PENDING_APPROVAL` / `READY` / `AWAITING_RFI` |

Test-enforced invariants: `set(actions[].obligation) == set(obligations[].kind)` (V-008), every action
has an owner and a due date, and the recommendation is in
`policy_admissibility.allowed_recommendations`.

## 7. Compatibility and versioning

* Schemas are versioned in their `$id` (`…/1.0.0/…`). Adding a required field is a **major** change and
  requires a migration for any stored artefact; adding an optional field is minor; tightening a
  constraint is major even when the field is optional.
* Artefacts carry the agent id and the policy version that produced them, so a stored profile can
  always be read against the policy it was built for — and replayed, or refused, deliberately.
* The policy pack's version (`cdd-policy-2026.4`) is part of the decision id. An envelope produced by
  a different pack cannot be quietly substituted: the replay check compares the id.
* `make contracts` validates every stored artefact against the current schemas. A schema change that
  orphans a stored artefact fails the build rather than the auditor.
