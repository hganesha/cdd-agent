# 00 · Agent definition — AXIOM-POLICY

**Agent id** `AXIOM-POLICY` · **Policy pack** `cdd-policy-2026.4` · **Engine** OPA v1.21.3 (embedded
binary at `bin/opa`) · **Owner** Financial Crime Policy, with Platform Security owning the runtime

---

## 1. What this agent is

AXIOM-POLICY is the **deterministic decision service** for customer due diligence. It takes a
structured description of a case — risk factors, ownership graph, screening results, evidence
coverage, jurisdiction — and returns a decision envelope: a closed-enum outcome, the reason codes that
fired, the obligations that follow, the human gate the case routes to, and a `decision_id` that binds
the whole thing to a policy version and an input hash.

It is written in Rego, evaluates inside OPA, and is compiled under a **restricted capability set**
that makes network and clock access impossible at the engine level (`http.send`, `time.now_ns`,
`opa.runtime` are not in the allow-list; the compile fails if a policy uses one). It is not a prompt,
not a description of a policy, and not a model output. The same input always produces the same output,
and that output is auditable three years later because the bundle is content-addressed and archived.

The architectural claim it exists to make: **the agent proposes, the policy decides.** A language
model may gather evidence, form findings and *recommend*; it may not clear a sanctions hit, may not
approve a case, may not lower a risk band. Those verbs are not in its capability vocabulary, and the
policy service will not accept them from it even if they were.

## 2. Division of labour

| Question | Owner | Never answered by |
|---|---|---|
| What is out there about this subject? | WEBIS-SEARCH (evidence pack) | this agent — it never reads the open internet |
| What does the evidence mean for this case? | CDD-INVESTIGATION (findings) | this agent — it has no view on facts |
| Is this treatment permitted, required or forbidden? | **AXIOM-POLICY** | any model, any prompt, any analyst's discretion without a record |
| Has the decision been approved by a human? | `agent.authority` (a separate Rego package) | the same package that made the decision |

Three properties follow, and they are the reason the split is drawn this way:

1. **No self-approval.** `policy.activate`, `case.approve`, `risk.override`, `sanctions.clear` are in
   `prohibited_actions` — no input, no token, no role makes them `allowed`. `agent.authority` can only
   return `approval_required` with a named approver and a TTL, or `denied`.
2. **No paraphrase.** `outcome` is a closed enum (`PERMIT`, `PERMIT_WITH_CONDITIONS`, `REQUIRE_RFI`,
   `REQUIRE_EDD`, `BLOCK`, plus `_INVALID_INPUT`). An agent cannot report a `BLOCK` as "requires
   further review".
3. **No drift.** The rules live in a bundle with a revision, a content hash and a manifest; the running
   service verifies the bundle before it serves a decision, and every decision record names the
   revision that produced it.

## 3. The decision envelope

```json
{
  "decision_id": "CASE-4831:cdd-policy-2026.4:sha256:abc123…",
  "policy_version": "cdd-policy-2026.4",
  "outcome": "BLOCK",
  "reasons": ["COVERAGE_BELOW_MINIMUM", "DECISION_BLOCKING_GAPS", "PEP_EXPOSURE",
              "RISK_BAND_FLOOR_APPLIED", "RISK_BAND_HIGH", "SCREENING_INCOMPLETE",
              "UNRESOLVED_BENEFICIAL_OWNERSHIP"],
  "obligations": [
    {"kind": "CLOSE_UBO_GAP", "because": "UNRESOLVED_BENEFICIAL_OWNERSHIP"},
    {"kind": "COMPLETE_SCREENING", "because": "SCREENING_INCOMPLETE"},
    {"kind": "HUMAN_EDD_APPROVAL", "because": "CDD_LEVEL_EDD"}],
  "gates": ["COVERAGE_BELOW_MINIMUM", "DECISION_BLOCKING_GAPS", "INJECTION_ATTEMPT",
            "RESIDUAL_HIGH_OR_ABOVE", "SCREENING_INCOMPLETE", "UNRESOLVED_UBO"],
  "risk": {"score": 37.95, "band": "HIGH", "weighted_score_band": "LOW", "cdd_level": "EDD",
           "factors_triggered": [...], "applied_overrides": [...]},
  "ubo": {"beneficial_owners": ["Elena Vasquez"], "effective_pct": {"Elena Vasquez": 37.0,
          "Viktor Sorensen": 18.0}, "unresolved_pct": 45.0, "control_prong_missing": false},
  "screening": {"screening_complete": false, "requires_adjudication": false, "pep_exposure": true},
  "coverage": {"coverage_pct": 16.7, "min_coverage_pct": 100, "blocking_rde_ids": ["RDE-07", "RDE-08"]},
  "route": "EDD_UNIT",
  "auto_final_allowed": false,
  "jurisdiction": {"overlay_applied": [...], "require_source_of_wealth": true},
  "agent_may_clear_sanctions_hit": false
}
```

Note `risk.score: 37.95` with `band: HIGH` and `weighted_score_band: LOW`: the weighted score would
place the case in `LOW`, but a **band floor** fired because an elevated-risk factor is present. That
is SOP §7.2 made executable — *"the firm must not apply a lower risk rating on the basis of a single
mitigating factor where an elevated-risk factor is present"* — and it is exactly the kind of judgement
that must not be left to a model.

## 4. Scope: what the policy pack decides

| Package | Question | Key rules |
|---|---|---|
| `cdd.risk` | What is the risk band, and does a floor override the weighted arithmetic? | 21 permitted factors, 5 weighted dimensions, 8 band floors, unknown-factor rejection |
| `cdd.ubo` | Who are the beneficial owners, does an ownership chain reach a natural person, is a nominee declared? | 25 % threshold, effective-percentage look-through to depth 4, control prong, unresolved-percentage gate |
| `cdd.screening` | Is screening complete, is there a sanctions hit, is adjudication required, is there PEP exposure? | 5 required lists, prohibition on agent clearance, lookalike handling |
| `cdd.coverage` | Is the evidence base sufficient for the requested CDD level? | per-level minimums (SDD 90, CDD 95, EDD 100), decision-blocking RDE ids |
| `cdd.gates` | Which human gates are forced, and where does the case route? | 8 forced gate conditions, `auto_final_allowed: false` |
| `jurisdiction.overlay` | Which jurisdiction-specific requirements apply? | source-of-wealth, local list additions |
| `agent.decision` | The composed envelope | closed-enum outcome, reasons, obligations, route |
| `agent.authority` | May the agent perform this action? | 11 prohibited, 5 gated (named approver + TTL), 9 allowed, unknown → denied |

## 5. Design positions

1. **Closed enums everywhere the agent can paraphrase.** Outcome, reason codes, obligation kinds, gate
   ids, route names. A model asked to "summarise the decision" produces prose; the decision itself
   cannot be prose.
2. **Unknown factors are rejected, not ignored.** `input.factors` containing an id the pack does not
   know makes the decision `INVALID_INPUT` / `BLOCK`. Otherwise an agent could invent a mitigating
   factor and silently reduce a score.
3. **Floors over arithmetic.** The weighted score is the *input* to banding, not the answer. Eight
   `band_floors` can raise a band and nothing can lower one; the `applied_overrides` are recorded so a
   reviewer sees *why* a 37.95 is `HIGH`.
4. **Unresolved ownership blocks.** 45 % unresolved + `blocks_conclusion` → `BLOCK`, whatever the
   ownership percentages say. Partial knowledge of a beneficial owner is not knowledge.
5. **Only humans approve.** `agent.authority` distinguishes `allowed` / `approval_required`
   (approver + TTL) / `denied`; `approval_required` results carry a matching rule so a
   *self*-approval request from the same agent identity fails.
6. **Every decision is evidence.** `DecisionLedger` writes a hash-chained record (`PZ-nnnnnn`) with the
   input hash, the decision, the bundle revision and the elapsed time. The decision is reproducible
   from the record + the archived bundle; nothing depends on the service's memory.
7. **Policy changes are code changes.** A clause of the SOP becomes a Rego diff, run in a sandbox
   against the corpus, and its impact is *measured* (which of the 10 corpus cases change and how)
   before a human can even see a DRAFT. There is no "ask the model to relax the threshold" path.
8. **The service can be exercised offline.** `bin/opa` is vendored; the bundle builds, verifies,
   activates and evaluates with no network; 35 Rego tests and 22 Python tests run in ~9 seconds.

## 6. Reference implementation

```
policy/data.json                 the pack: thresholds, weights, bands, floors, lists, gates, authority
policy/cdd/{risk,ubo,screening,coverage,gates}.rego
policy/jurisdiction/overlay.rego
policy/agent/{decision,authority}.rego
policy/tests/{decision_test,authority_test}.rego      35 tests
src/axiom/opa.py                 OPAClient — restricted capabilities, eval, explain, compile
src/axiom/evidence.py            DecisionLedger — hash chain, PZ-nnnnnn, idempotency
src/axiom/bundle.py              build · verify · stage · activate (named approver required)
src/axiom/author.py              SOP clause → sandbox patch → validate → impact → DRAFT proposal
src/axiom/cli.py                 decide · authority · check · test · build · verify · author · corpus · ledger
schemas/                         decision, authority, bundle-manifest, policy-proposal
corpus/SOP_CDD_v4.2_excerpt.md   the source document policy changes are derived from
corpus/decisions/*.json          10 fixtures with pinned expected outcomes
tests/test_policy_agent.py       22 tests, including Rego↔Python risk-engine parity
bin/opa                          OPA v1.21.3, vendored
```

## 7. Definition of done

* `opa test policy/` → **PASS: 35/35**.
* `python3 tests/test_policy_agent.py` → **22/22 passed**, including
  `test_policy_compiles_under_restricted_capabilities` (injecting `http.send` into a policy copy
  breaks the compile), `test_python_and_opa_risk_engines_agree` (±0.01 across 8 factor combinations),
  bundle tamper detection, evidence-chain tamper detection, and author gating (validation failure,
  precondition mismatch, impact measurement).
* `python3 -m axiom.cli corpus` → the 10-case baseline, each matching its pinned expectation.
* `python3 -m axiom.cli decide --input /tmp/decisive.json` → the envelope in §3 (this fixture is the
  documented `CASE-4831` shape: `decision_id` binds case id + policy version + input hash; the hash
  shown is a placeholder for the fixture, not a real digest).
* `python3 -m axiom.cli authority --action case.approve` → `denied` (and exit 3).
