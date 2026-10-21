# 03 · I/O contracts

Four schemas, all JSON Schema 2020-12, in `schemas/`. The decision schema is the tightest artefact in
the whole project: it is what stops a permissive reading of a deterministic result.

| Schema | Direction | Enforced by |
|---|---|---|
| `decision.schema.json` | out (the envelope an agent consumes) | `axiom.cli decide`, `tests/test_policy_agent.py` |
| `authority.schema.json` | out (may the agent do this?) | `axiom.cli authority`, authority tests |
| `bundle-manifest.schema.json` | out (what is running) | `axiom.bundle` build/verify |
| `policy-proposal.schema.json` | out (what a change would do) | `axiom.author` before writing a DRAFT |

---

## 1. DecisionRequest (input)

There is no schema file for the input because the *policy* is the schema: `cdd.risk` rejects unknown
factor ids, `cdd.ubo` reads a fixed ownership shape, `cdd.screening` enumerates required lists. An
input that does not fit leaves `result` undefined or `valid_input: false`, and the envelope says so
rather than guessing. The shape, as used by all 10 corpus fixtures:

```json
{
  "case": {"case_id": "CASE-4831", "cdd_level_requested": "EDD",
           "jurisdiction_profile": "EU_AMLD"},
  "input_hash": "sha256:…",
  "evaluate_at": "2026-10-21T00:00:00Z",
  "critic_status": "PASS",              // PASS | FAIL   → forces CRITIC_FAIL gate
  "injection_attempt": false,           // → forces INJECTION_ATTEMPT gate
  "factors": {"F-CUST-01": true, "F-GEO-01": true},
  "risk": {"cdd_level": "EDD"},
  "ownership": {
    "paths": [{"person": "Elena Vasquez", "pcts": [60, 62]}],
    "unresolved": [{"holder": "Nominee Ltd", "pct": 45}],
    "direct_holdings": [{"holder": "P1", "pct": 10}],
    "nominee_declared": false
  },
  "screening": {
    "queries": [{"list": "OFAC_SDN"}, {"list": "UN_CONSOLIDATED"}, {"list": "EU_CFSP"},
                {"list": "UK_OFSI"}, {"list": "PEP_GLOBAL"}],
    "hits": [{"list": "PEP_GLOBAL", "strength": "TRUE", "matched_name": "Elena Vasquez"}],
    "lists_unavailable": []
  },
  "coverage": {"elements": [{"rde_id": "RDE-01", "status": "SATISFIED"},
                            {"rde_id": "RDE-07", "status": "MISSING"}]}
}
```

Two things a caller cannot do:

* **Supply free text.** Every consumed field is typed; there is no notes field, no "explanation", no
  override slot. An injected sentence has nowhere to sit.
* **Supply a factor the pack does not know.** `unknown_factors` → `valid_input: false` →
  `INVALID_INPUT`. This is the guard against an agent inventing a mitigation.

## 2. Decision (output) — `schemas/decision.schema.json`

Required: `decision_id`, `policy_version`, `outcome`, `reasons`, `obligations`, `gates`, `route`,
`risk`, `ubo`, `screening`, `coverage`, `jurisdiction`, `agent_may_clear_sanctions_hit`.

```json
{
  "decision_id": "<case_id>:<policy_version>:sha256:<input digest>",
  "outcome": "PERMIT | PERMIT_WITH_CONDITIONS | REQUIRE_RFI | REQUIRE_EDD | BLOCK | INVALID_INPUT",
  "reasons": ["CLOSED_ENUM_CODE", …],
  "obligations": [{"kind": "CLOSE_UBO_GAP", "because": "UNRESOLVED_BENEFICIAL_OWNERSHIP"}],
  "gates": ["UNRESOLVED_UBO", …],
  "route": "MLRO | EDD_UNIT | COMPLIANCE_OFFICER | CASE_ANALYST",
  "auto_final_allowed": false,
  "risk": {"score": 37.95, "band": "HIGH", "weighted_score_band": "LOW", "cdd_level": "EDD"},
  "agent_may_clear_sanctions_hit": false
}
```

Pins that matter more than the rest of the file:

| Pin | Why |
|---|---|
| `outcome` is an `enum`, not a string | an agent cannot paraphrase a `BLOCK` into "further review" |
| `obligations[].kind` is an `enum` | obligations are actionable by a workflow engine; free-form obligations are not |
| `gates[]` items are an `enum` of condition names | the gate a case must clear is legible without a lookup table |
| `route` is an `enum` | routing is a policy decision, not an assignment |
| `auto_final_allowed: const false` | no deployment can enable straight-through finalisation |
| `agent_may_clear_sanctions_hit: const false` | an agent that reports `true` produces an **invalid document** |
| `decision_id` pattern | binds case + policy version + input digest; identical input → identical id |

If the schema rejects a decision, the decision is not delivered. `axiom.cli decide` validates before
writing `out/last_decision.json` and before appending to the ledger.

## 3. Authority — `schemas/authority.schema.json`

```json
{"action": "case.approve",
 "decision": "allowed | approval_required | denied",
 "approver": "COMPLIANCE_OFFICER | null",
 "ttl_hours": 8,
 "reason": "prohibited_action | gated_action | allowed_action | unknown_action",
 "evidence_id": "PZ-000011"}
```

`approver` and `ttl_hours` are required when `decision == "approval_required"` (schema `allOf/if-then`),
and forbidden when `denied` — a denial that carries an approver is a contradiction the schema refuses.

## 4. Bundle manifest — `schemas/bundle-manifest.schema.json`

```json
{"revision": "r-001", "policy_version": "cdd-policy-2026.4",
 "sha256": "sha256:6b823023…", "size_bytes": 10678,
 "built_at": "2026-10-21T15:17:24.262Z", "agent_id": "AXIOM-POLICY",
 "opa_version": "1.21.3",
 "entrypoints": ["agent/decision/result", "agent/authority/result", "cdd/risk/result"],
 "files": {"agent/decision.rego": "sha256:…", …}}
```

`verify` recomputes every `files[]` hash and the archive hash; `activate` refuses a manifest whose
hashes do not match, and records the approver and ticket in the ledger. `out/bundles/ACTIVE` is a
pointer file naming the revision currently served.

## 5. Policy proposal — `schemas/policy-proposal.schema.json`

```json
{"proposal_id": "P-2026-012",
 "clause": {"source": "SOP_CDD_v4.2_excerpt.md", "section": "5.4", "text": "…"},
 "change": {"file": "cdd/ubo.rego", "kind": "add_rule", "rule": "…", "rationale": "…"},
 "precondition": "data.config.thresholds.ubo_pct == 25",
 "validation": {"compiled": true, "suite": "35/35", "restricted_capabilities": true},
 "impact": {"cases_evaluated": 10, "changed": 3, "diffs": […]},
 "materiality": "LOW | MEDIUM | HIGH",
 "status": "DRAFT",
 "approved_by": null, "activated_in_revision": null}
```

`status` is `const "DRAFT"`: a proposal object cannot express activation. Activation is a *bundle*
operation with an approver, and it stamps `activated_in_revision` on the proposal afterwards — a
different actor, a different record, a different day.

## 6. Ledger record

`out/decision_evidence.jsonl`, one record per decision / authority query / bundle operation:
`{evidence_id: PZ-nnnnnn, seq, created_at, record_type, decision_id?, input_hash, decision,
policy_version, bundle_revision, elapsed_ms, prev_hash, record_hash, signature}`.
`record_type ∈ {DECISION, AUTHORITY, BUNDLE, PROPOSAL}`. Chain rules are identical to the search
agent's ledger (`spec/04` §6): canonical JSON, `prev_hash`, tamper detection at the first divergent
`seq`.

## 7. Versioning

* Decision schema change → new `policy_version` (`cdd-policy-2026.4` → `…2026.5`) and a new bundle
  revision. Consumers key on `policy_version`, so an untested consumer cannot silently absorb a
  changed envelope.
* Adding an enum value (a new reason code or obligation) is a **minor** policy version; removing or
  renaming one is **major** and requires the consumer to be updated first — an old consumer meeting an
  unknown reason code must treat it as `BLOCK`, which the CDD agent's consumer stub asserts.
