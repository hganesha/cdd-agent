# 04 · Evidence writes

A decision that cannot be reproduced is not a decision; it is an opinion with a timestamp. This
document specifies what AXIOM-POLICY writes, into which store, keyed how, and what an auditor can
reconstruct from the record alone.

## 1. The reconstruction claim

Given (a) a `PZ-nnnnnn` ledger record, (b) the archived bundle for the `bundle_revision` it names and
(c) the input payload — anyone can re-run the decision and must get byte-identical output.

```
ledger record  ──►  bundle_revision ──►  out/bundles/<rev>.tar.gz  (sha256 pinned in the manifest)
      │                                          │
      │ input_hash                               │ policy + data.json
      └──────────────►  OPA eval  ◄──────────────┘
                            │
                            ▼
                   decision == ledger.decision   (required, checked by `axiom.cli ledger --replay`)
```

The ledger stores the *decision*, not just a hash of it, precisely so that the check is possible
without a running service and without the case file.

## 2. Record types

| `record_type` | Written by | Payload |
|---|---|---|
| `DECISION` | `cli decide` / `policy.evaluate` | `decision_id`, `input_hash`, `outcome`, `reasons`, `obligations`, `route`, `risk{score,band,cdd_level}`, `policy_version`, `bundle_revision`, `elapsed_ms` |
| `AUTHORITY` | `cli authority` / every capability check | `action`, `decision`, `approver?`, `ttl_hours?`, `actor`, `reason` |
| `BUNDLE` | `build` / `verify` / `activate` | `revision`, `sha256`, `files`, `event` (`BUILT`/`VERIFIED`/`ACTIVATED`/`SUPERSEDED`), `approver?`, `ticket?` |
| `PROPOSAL` | `author` | `proposal_id`, `clause`, `validation`, `impact`, `materiality`, `status` |
| `TEST_RUN` | `cli test` / `author` | `scope`, `passed`, `failed`, `detail` |
| `STATE` | bundle staging, sandbox lifecycle | `event`, `detail` |

`AUTHORITY` records include **denials**. A run of denied `case.approve` attempts from an agent identity
is a security signal, and dropping it would erase the only trace of the attempt.

## 3. Record anatomy

```json
{"evidence_id": "PZ-000010", "seq": 10, "agent_id": "AXIOM-POLICY",
 "record_type": "DECISION", "decision_id": "CASE-4831:cdd-policy-2026.4:sha256:abc123",
 "input_hash": "sha256:…", "input_fields": ["case", "coverage", "factors", "…"],
 "bundle": {"revision": "r-001", "sha256": "sha256:…", "policy_version": "cdd-policy-2026.4"},
 "opa_version": "1.21.3", "engine": "data.agent.decision.result",
 "outcome": "BLOCK",
 "reasons": ["COVERAGE_BELOW_MINIMUM", "…", "UNRESOLVED_BENEFICIAL_OWNERSHIP"],
 "obligations": ["CLOSE_UBO_GAP", "COMPLETE_SCREENING", "HUMAN_EDD_APPROVAL"],
 "route": "EDD_UNIT", "gates": ["UNRESOLVED_UBO", "…"],
 "risk": {"score": 37.95, "band": "HIGH", "cdd_level": "EDD"}, "elapsed_ms": 4.31,
 "prev_hash": "sha256:…", "record_hash": "sha256:…", "signature": "kms-stub:…"}
```

## 4. Ordering and idempotency

* A decision record is written **after** the envelope validates against `schemas/decision.schema.json`.
  An invalid envelope is an error, not a record — the ledger never contains a decision that the
  contract would reject.
* `key = ("decision", decision_id, sha256(input))`; `decision_id` already binds case id, policy
  version and input digest, so re-running the same input against the same policy returns the **same**
  `evidence_id` and appends nothing. This makes evaluation
  idempotent, which matters because a retry loop is normal and a duplicated decision history is not.
* A *changed* decision for the same input and policy is impossible by construction (determinism). If
  the policy changed, the key changes, and both records stay — the pair is the evidence that a policy
  change moved a case.
* Bundle operations are keyed on `(revision, event)` so a repeated `verify` does not spam the chain.

## 5. Tamper evidence

Identical mechanism to the search agent's ledger, deliberately: canonical JSON (sorted keys, no
whitespace), `record_hash = sha256(record − {record_hash, signature})`, `prev_hash` = previous line,
genesis = `GENESIS`.

```bash
python3 -m axiom.cli ledger          # {"chain_valid": true, "head": "sha256:…", "records": n}
python3 -m axiom.cli replay --input /tmp/decisive.json
# {"reproduced": true, "evidence_id": "PZ-000010", "bundle_revision": "r-001",
#  "compared_fields": ["outcome", "reasons", "route"], "differences": []}
```

`replay` is the reproducibility proof: it re-evaluates the supplied input against the policy currently
on disk and compares the result with the record the ledger holds for that `decision_id`. A policy
change that moves a case shows up as a non-empty `differences` — which is exactly what you want it to
do, and exactly why the check must be run rather than assumed.

Tested behaviour: mutating an outcome from `PERMIT` to `PERMIT_WITH_CONDITIONS` in a stored record
breaks the chain at that `seq` (`test_decision_ledger_detects_tampering`). The verifier never repairs.

Residual risk, stated: `signature` is `kms-stub:<hash prefix>`. The chain detects editing; it does not
yet provide non-repudiation against someone who can rewrite the whole file. A production deployment
signs `record_hash` with a key the service cannot export.

## 6. What is deliberately *not* written

| Not written | Why |
|---|---|
| The full case payload | minimisation: it contains names and ownership percentages. The record keeps `input_hash` (which binds it to the case file, retained separately under SOP §8.1) and `input_fields` (which proves nothing unexpected was consumed) — see §8 |
| Free-text rationale from any model | the decision's justification is its reason codes and its gate list, both enums |
| The evidence pack from WEBIS-SEARCH | the policy agent never reads it; a pack in this ledger would imply a causal link that does not exist |
| A "confidence" or "recommendation" field | those belong to the CDD agent's findings, which are inputs, not decisions |
| Deleted or superseded records | append-only; supersession is a new record (`SUPERSEDED`) naming what it replaced |

## 7. Retention

Decisions: 5 years after the relationship ends (SOP §8.1). Bundles: the revision named in any
retained decision must be retained for the same period — a decision whose policy cannot be produced is
not reproducible, so bundle retention is derived from ledger retention, not from a separate schedule.
Proposals: retained with their outcome, including the rejected ones (`P-2026-011` failed validation and
is kept: what was attempted is part of the control history).
