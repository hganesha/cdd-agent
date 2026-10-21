# AXIOM-POLICY

The deterministic decision service for customer due diligence. It takes a structured case — risk
factors, ownership graph, screening results, evidence coverage, jurisdiction — and returns a decision
envelope from **OPA/Rego**: a closed-enum outcome, the reason codes that fired, the obligations that
follow, the human gate the case routes to, and a `decision_id` binding all of it to a policy version
and an input digest.

**The agent proposes. The policy decides.** No model is in the decision path. The rules compile under a
restricted capability set that makes network, clock and filesystem access impossible at the engine
level, every decision is written to a hash-chained ledger, and no code path can activate a policy
change without a named human approval.

```
case facts ──► OPA (policy/cdd/*, policy/agent/*) ──► decision envelope ──► evidence record (PZ-nnnnnn)
                          ▲                                    ▲
                          │                                    └── narration (prompt, cosmetic only)
              bundle (hash-verified)  ◄── human approval ◄── DRAFT proposal ◄── SOP clause
```

## Quickstart

```bash
cd policy-agent

make check        # opa test 35/35 + 22 python properties + 10-case corpus + contracts
make decide       # evaluate the canonical CASE-4831 fixture and print the envelope
make corpus       # 10-case regression against the pinned baseline
make authority    # ACTION=case.approve → denied (exit 3)
make ledger       # verify the evidence chain
make replay       # reproduce the last decision from its input
```

Requirements: Python 3.11+, `jsonschema`, `pyyaml`; OPA v1.21.3 vendored at `bin/opa` (falls back to
`opa` on `PATH`). No network access is needed at any point.

## What a decision looks like

```
$ make decide

{ "decision_id": "CASE-4831:cdd-policy-2026.4:sha256:abc123",
  "outcome": "BLOCK",
  "reasons": ["COVERAGE_BELOW_MINIMUM", "DECISION_BLOCKING_GAPS", "PEP_EXPOSURE",
              "RISK_BAND_FLOOR_APPLIED", "RISK_BAND_HIGH", "SCREENING_INCOMPLETE",
              "UNRESOLVED_BENEFICIAL_OWNERSHIP"],
  "obligations": [{"kind": "CLOSE_UBO_GAP", "because": "UNRESOLVED_BENEFICIAL_OWNERSHIP"},
                  {"kind": "COMPLETE_SCREENING", "because": "SCREENING_INCOMPLETE"},
                  {"kind": "HUMAN_EDD_APPROVAL", "because": "CDD_LEVEL_EDD"}],
  "risk":   {"score": 37.95, "band": "HIGH", "weighted_score_band": "LOW", "cdd_level": "EDD"},
  "ubo":    {"beneficial_owners": ["Elena Vasquez"], "unresolved_pct": 45.0},
  "coverage": {"coverage_pct": 16.7, "min_coverage_pct": 100},
  "gates": {"required_gates": ["UNRESOLVED_UBO", "RESIDUAL_HIGH_OR_ABOVE", "…"],
            "route_to": "EDD_UNIT", "blocks_final_status": true},
  "agent_may_clear_sanctions_hit": false }
```

Read the contradiction in `risk`: score **37.95**, band **HIGH**, weighted band **LOW**. A weighted
score of 37.95 *is* `LOW`; a **band floor** fired because a PEP factor is present — SOP §7.2, "the firm
must not apply a lower risk rating on the basis of a single mitigating factor where an elevated-risk
factor is present", made executable. Nothing can lower a band; only eight named floors can raise one,
and each is recorded in `applied_overrides`.

## The corpus baseline

| case | outcome | band | level |
|---|---|---|---|
| `clean_corporate` · `clean_individual` | PERMIT | LOW | SDD |
| `eu_profile_sow` | PERMIT_WITH_CONDITIONS | LOW | SDD |
| `pep_principal` | REQUIRE_EDD | HIGH | EDD |
| `high_risk_geography` | BLOCK | VERY_HIGH | EDD |
| `nominee_unresolved` | BLOCK | LOW | SDD |
| `ubo_22pct_single` · `ubo_24pct_two_paths` | BLOCK | LOW | SDD |
| `sanctions_true_hit` | BLOCK | LOW | SDD |
| `sanctions_lookalike` | PERMIT | LOW | SDD |

Pinned in `corpus/expected.json`. `make corpus` exits non-zero on drift: a changed outcome without a
declared policy change is a failing test, not a new baseline.

## Layout

```
spec/00-agent-definition.md   what the service is; the three-way split; design positions
spec/01-system-prompt.md      why there is no prompt in the decision path; the two peripheral prompts
spec/02-skills.md             9 skills × inputs / rules / outputs / evidence / failure / tests
spec/03-io-contracts.md       decision · authority · bundle-manifest · policy-proposal schemas
spec/04-evidence-writes.md    ledger records, keys, ordering, tamper detection, replay
spec/05-long-running-execution.md  latency, portfolio re-decide, authoring loop, failure handling
spec/06-guardrails.md         structural controls, change control, input trust, residual risks
spec/07-evaluation.md         3 test layers, corpus rationale, SOP coverage matrix

prompts/system.md             SYS-NARRATE · SYS-AUTHOR (hash-pinned with data.json)
prompts/pins.json             digests; `make check-prompt` fails on an unpinned edit

policy/data.json              the pack: thresholds, weights, bands, floors, lists, gates, authority
policy/cdd/risk.rego          21 factors, 5 dimensions, band floors, unknown-factor rejection
policy/cdd/ubo.rego           25 % threshold, look-through to depth 4, control prong, nominee
policy/cdd/screening.rego     5 required lists, blocking strengths, no agent clearance
policy/cdd/coverage.rego      per-level coverage minimums, decision-blocking RDEs
policy/cdd/gates.rego         9 gate conditions, MLRO → EDD → Compliance routing
policy/jurisdiction/overlay.rego   adds requirements, never relaxes them
policy/agent/decision.rego    the composed envelope (entrypoint)
policy/agent/authority.rego   11 prohibited · 5 gated · 9 allowed · default deny
policy/tests/                 35 Rego tests

src/axiom/opa.py              OPAClient: restricted capabilities, eval, explain, compile
src/axiom/evidence.py         DecisionLedger: PZ-nnnnnn, hash chain, idempotency
src/axiom/bundle.py           build · verify · stage · activate (named approver + ticket)
src/axiom/author.py           SOP clause → sandbox → validate → impact → DRAFT
src/axiom/cli.py              decide · replay · authority · check · test · build · verify · activate
                              · author · corpus · ledger
schemas/                      4 JSON Schema 2020-12 contracts
tests/test_policy_agent.py    22 end-to-end properties
scripts/check_contracts.py    what `make check` runs after the tests
scripts/check_authority.py    asserts config/authority.yaml has not drifted from the Rego
corpus/SOP_CDD_v4.2_excerpt.md  the source document for policy changes
bin/opa                       OPA v1.21.3 (vendored so the package is runnable offline)
```

## The five properties worth knowing

1. **No model in the decision path.** Rego + OPA, restricted capabilities, no `http.send`, no
   `time.now_ns`, no `opa.runtime`. `test_policy_compiles_under_restricted_capabilities` proves the
   check is live by injecting `http.send` into a policy copy and watching the compile fail.
2. **No agent can clear a sanctions hit.** `agent_may_clear_sanctions_hit` is `const false` in the
   decision schema, and `sanctions.clear` is in `prohibited_actions`. An agent that reports otherwise
   produces an invalid document.
3. **No straight-through finalisation.** `auto_final_allowed: const false`; any fired gate condition
   sets `blocks_final_status`.
4. **Unknown means denied.** Unknown action → `denied`. Unknown risk factor → `INVALID_INPUT` → BLOCK.
   Undefined result → CLI exit 2. A policy mistake fails closed.
5. **Change control with teeth.** A clause becomes a candidate Rego diff, run in a sandbox (compile +
   35 tests), its impact measured over the corpus, its materiality labelled, and it lands as `DRAFT`.
   Activation is a separate call requiring `approver` + `ticket`, written to the evidence chain.

## Two independent engines agree

The CDD investigation agent carries its own Python implementation of the same risk methodology
(`../cdd-investigation-agent/src/cdd_agent/policy.py`). `test_python_and_opa_risk_engines_agree` loads
it, generates 8 factor combinations, and asserts agreement with the Rego engine to **±0.01** on the
final score. That is a genuine cross-implementation check, not a test that compares a value with
itself — and it is why the risk arithmetic can be trusted when a case turns on a 0.05-point band
boundary.

## Audit trail

```bash
make ledger    # chain_valid: true, head: sha256:…
make replay    # reproduced: true, compared_fields: [outcome, reasons, route]
```

Every decision, authority query (including denials), bundle operation and proposal is a `PZ-nnnnnn`
record in `out/decision_evidence.jsonl`: canonical JSON, `prev_hash` chaining, tamper detection at the
first divergent `seq`, no delete and no update. `replay` re-evaluates a stored decision's input against
the policy on disk and compares — the check that makes the ledger meaningful rather than decorative.

The signature is a `kms-stub`: the chain detects editing; it does not yet provide third-party
non-repudiation. Stated in `spec/04 §5` and `spec/06 §7`, not hidden.

## Limits, stated plainly

This proves the implementation matches the policy; it does not prove the policy matches the
regulation — that is the clause-to-proposal loop plus human approval. The corpus is 10 hand-chosen
cases, not a sample of the book. A one-line threshold edit in `data.json` deserves the same review as a
new rule. See `spec/07-evaluation.md §6`.

## Siblings

* `../cdd-investigation-agent/` — the investigation agent whose findings are this service's input.
* `../external-search-agent/` — the evidence gatherer. It never talks to this service; the CDD agent
  hands facts over and gets a decision back with an evidence record.
