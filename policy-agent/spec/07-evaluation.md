# 07 · Evaluation

A deterministic service is evaluated differently from a model. There is no accuracy metric to
optimise: the questions are *does the policy say what the SOP says*, *does the implementation match the
policy*, and *does the evidence prove both*.

## 1. Three layers of testing

| Layer | Question | Artefact | Count |
|---|---|---|---|
| **Rule tests** (Rego) | Does each rule fire when it should, and only then? | `policy/tests/*_test.rego` | 35 |
| **End-to-end properties** (Python) | Do the guarantees hold across the whole envelope — schema, determinism, parity, tamper, authoring? | `tests/test_policy_agent.py` | 22 |
| **Corpus regression** | Do the 10 representative cases produce their pinned outcomes? | `corpus/decisions/*.json` + `axiom.cli corpus` | 10 |

```
opa test policy/                     ->  PASS: 35/35
python3 tests/test_policy_agent.py   ->  22/22 passed   (~9 s)
python3 -m axiom.cli corpus          ->  10/10 match
```

## 2. The corpus, and what each case pins

| Case | Outcome | Band | Level | Why it is in the corpus |
|---|---|---|---|---|
| `clean_corporate` | PERMIT | LOW | SDD | the happy path; if this ever blocks, the pack is over-restrictive |
| `clean_individual` | PERMIT | LOW | SDD | identical treatment for a person and an entity |
| `eu_profile_sow` | PERMIT_WITH_CONDITIONS | LOW | SDD | jurisdiction overlay adds an obligation without changing the band |
| `pep_principal` | REQUIRE_EDD | HIGH | EDD | **floor**: weighted score is low, PEP factor raises the band |
| `high_risk_geography` | BLOCK | VERY_HIGH | EDD | **floor**: FATF call-for-action nexus |
| `nominee_unresolved` | BLOCK | LOW | SDD | 45 % unresolved ownership blocks despite a LOW band |
| `ubo_22pct_single` | BLOCK | LOW | SDD | below threshold → control prong required |
| `ubo_24pct_two_paths` | BLOCK | LOW | SDD | two 24 % paths do not add up to a 25 % owner |
| `sanctions_true_hit` | BLOCK | LOW | SDD | blocking hit → MLRO route |
| `sanctions_lookalike` | PERMIT | LOW | SDD | a WEAK/LOW hit must not block; over-blocking is a real cost |

Each fixture carries `input` + `expected {outcome, band, cdd_level, reasons}`. The runner asserts all
four; a mismatch prints the case name and the diff, and exits non-zero.

## 3. Properties the Python suite asserts

| Property | Test | Why it matters |
|---|---|---|
| Decision validates against the schema | `test_decision_matches_schema` | the envelope is a contract, not a dict |
| `agent_may_clear_sanctions_hit` can never be true | `test_agent_cannot_clear_a_sanctions_hit` | the central prohibition |
| Same input → same `decision_id` and same envelope | `test_decision_is_deterministic` | reproducibility; also makes the ledger key sound |
| Unknown factor → `INVALID_INPUT` + BLOCK | `test_unknown_factor_is_rejected` | no invented mitigations |
| Rego runs under restricted capabilities | `test_policy_compiles_under_restricted_capabilities` | no network/clock smuggling |
| Rego suite is 35/35 | `test_rego_suite_passes` | the count is asserted, so a deleted test fails |
| **Rego risk engine == Python risk engine** | `test_python_and_opa_risk_engines_agree` | two independent implementations, ±0.01 across 8 factor combinations |
| Authority: deny / gate / allow | `test_authority_denies_and_gates` | capability vocabulary |
| Bundle builds, verifies, tampering detected | `test_bundle_build_and_verify`, `test_bundle_tamper_is_detected` | what runs is what was approved |
| Activation needs a named approver + ticket | `test_activation_requires_a_named_approval` | no self-activation |
| Author: validation failure blocks staging | `test_author_refuses_a_precondition_mismatch` | a bad candidate cannot reach DRAFT-as-valid |
| Author: impact analysis is measured | `test_author_impact_analysis_changes_the_right_cases` | predicts nothing it did not compute |
| Evidence chain tamper detection | `test_decision_ledger_detects_tampering` | audit integrity |
| Replay reproduces a stored decision | `axiom.cli replay` | the ledger is a record of *this* policy |

The parity test deserves its own paragraph. The CDD investigation agent carries an independent Python
model of the same risk methodology (`src/cdd_agent/policy.py`). It was written for a different purpose
and by a different route. `test_python_and_opa_risk_engines_agree` loads it, generates 8 factor
combinations, and asserts agreement to ±0.01 on the final score. That is a genuine cross-check of both
implementations rather than a test that compares a value with itself.

## 4. Coverage of the SOP

| SOP clause | Implemented in | Test |
|---|---|---|
| 5.1 identify every owner ≥ 25 % | `cdd/ubo.rego` (`ubo_pct`) | `test_ubo_threshold_is_inclusive_at_25_pct`, `ubo_22pct_single` |
| 5.2 control prong where no owner reaches 25 % | `cdd/ubo.rego` (`control_prong_missing`) | `ubo_22pct_single` |
| 5.3 nominee requires a declaration | `cdd/ubo.rego` | `nominee_unresolved` |
| 5.4 incomplete chain → EDD unit | `cdd/ubo.rego` (`blocks_conclusion`) | `nominee_unresolved` |
| 6.1 screen all owners/directors/signatories | `cdd/screening.rego` (required lists) | `test_screening_fixture_derives_queries_from_required_lists` |
| 6.2 sanctions match → MLRO, not analyst | `cdd/screening.rego`, `cdd/gates.rego` | `sanctions_true_hit` |
| 6.3 unavailable list ≠ cleared | `cdd/screening.rego` (`lists_unavailable`) | decision test |
| 7.1 rating recorded with its factors | `cdd/risk.rego` (`trace`, `triggered_factors`) | schema + corpus |
| 7.2 no lower band where an elevated factor is present | `cdd/risk.rego` (`band_floors`) | `pep_principal`, `high_risk_geography` |
| 7.3 EDD for high risk | `cdd/risk.rego` (`cdd_level`) | `pep_principal` → EDD |
| 8.1 rationale retained | decision record + ledger | `test_decision_ledger_*` |

An uncovered clause is a defect with a name. `axiom author` starts from this table: a clause with no
mapping is a candidate; a clause already implemented is refused (`_implemented()`), which prevents
duplicate rules.

## 5. Metrics that are *not* used

* **No accuracy/ROC/AUC.** There is no ground-truth label for "should this customer be blocked"; the
  policy defines it. Reporting a confusion matrix against the policy itself would be circular.
* **No model-quality metrics.** There is no model in the path.
* **No latency SLO as a quality gate.** Latency is measured (§05) but is not a correctness property;
  a fast wrong decision is worse than a slow right one.

What *is* reported to the business, monthly: outcome distribution by band, number of cases blocked
solely by a band floor, gate clearance times by route, and the count of `INVALID_INPUT` decisions
(a rising count means an upstream agent is emitting malformed input — a signal about the *other* agent).

## 6. What this suite cannot tell you

1. **Whether the policy is right.** It proves implementation ≡ policy. Policy ≡ regulation is a
   human, documented judgement made in `corpus/SOP_CDD_v4.2_excerpt.md` and the change control around
   it.
2. **Whether the corpus is representative.** Ten cases were chosen for their failure modes, not by
   sampling the book.
3. **Whether an approval was wise.** The chain proves an approver existed; it cannot prove they read
   the diff.
4. **Nothing about the classifier upstream.** If the CDD agent mislabels a fact, this service applies
   the policy correctly to a wrong input. That boundary is why the coverage skill exists.

## 7. Regression protocol

```bash
make test          # opa test + python suite
make corpus        # 10-case baseline, pinned expectations
make decide        # the canonical CASE-4831 envelope, printed
make replay        # reproduce the last decision from its input
make ledger        # chain validity
```

Any change to `policy/**` or `policy/data.json` requires: all three test layers green, an updated
corpus if a pinned outcome legitimately moves (with the reason recorded in the case file), a new bundle
revision, and a named approval. A pinned outcome that changes without a corresponding corpus edit is a
failing test — which is the entire purpose of pinning it.
