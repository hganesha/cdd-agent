# 06 · Guardrails

The policy agent's guardrails are unusual in one respect: most of them are **structural**. There is no
model to persuade, no context to poison, no temperature to nudge. What remains is the set of ways a
deterministic service can still be made to do the wrong thing — by a bad input, a bad deploy, a bad
rule, or a caller that treats "no decision" as "no objection".

## 1. Structural controls

| Control | Mechanism | Test |
|---|---|---|
| No network, no clock, no filesystem in policy | OPA compile under an allow-listed capability set; `http.send`, `time.now_ns`, `opa.runtime` are not present | `test_policy_compiles_under_restricted_capabilities` (injects `http.send` into a copy → compile fails) |
| No self-approval | `case.approve`, `risk.override`, `sanctions.clear`, `policy.activate`, `policy.self_modify`, `evidence.delete`/`mutate`, `report.file`, `account.freeze`, `case.close`, `network.arbitrary` are `prohibited_actions` | `authority_test.rego` (all prohibited denied for every actor) |
| Default deny | unknown action → `denied`; unknown risk factor → `INVALID_INPUT` | `test_unknown_action_is_denied`, `test_unknown_factor_is_rejected` |
| Fail closed | undefined result is an error (exit 2), never an implicit permit; every fallible rule has a `default` | `cli decide` contract; `test_missing_default_would_break_the_decision` (author sandbox) |
| Closed enums | outcome, reasons, obligations, gates, routes are enums in `schemas/decision.schema.json` | `test_decision_matches_schema` |
| No straight-through finalisation | `auto_final_allowed: const false`, and gates `blocks_final_status` when any condition fires | `test_auto_final_is_disabled` |
| No agent clearance of sanctions | `agent_may_clear_sanctions_hit: const false` in the schema; `sanctions.clear` prohibited | `test_agent_cannot_clear_a_sanctions_hit` |

## 2. Policy change control

A policy change is the highest-risk operation in this package: it silently changes the treatment of
every case at once. The controls:

1. **Change = code.** Rules live in Rego under version control; `data.json` (thresholds, weights,
   bands, floors, lists, gates, authority tables) is pinned by hash alongside the prompt, because a
   threshold edit is a policy change even when no `.rego` file moves.
2. **Sandbox first.** `author` applies a candidate to a copy; the live tree is never edited by the
   authoring path.
3. **Validation gate.** `opa check` under restricted capabilities + the full Rego suite + the corpus.
   A candidate that fails any of these cannot be staged.
4. **Measured impact.** Every corpus case is re-evaluated and the diffs are reported. The model's
   *prediction* of the effect is compared with the measured effect; a mismatch is surfaced, not
   smoothed over.
5. **Materiality.** Blast radius (how many cases, which direction) is labelled LOW/MEDIUM/HIGH and
   shown to the approver before they see the diff.
6. **Named human activation.** `bundle.activate(approver, ticket)`; the approver and ticket are written
   to the evidence chain. No code path activates a bundle without them.
7. **Reversibility.** The previous revision is retained and marked `SUPERSEDED`; rollback is an
   activation of the prior revision, itself an auditable event.

## 3. Input trust

The policy agent consumes structured JSON only. The properties that keep that safe:

* **No free-text field** anywhere in the decision input. There is nowhere for an instruction to sit.
* **Typed factors.** A factor id the pack does not define is rejected, so an agent cannot invent a
  mitigating factor or a new risk dimension.
* **Derived, not supplied, quantities.** `unresolved_pct`, `coverage_pct` and the weighted score are
  computed from primitive inputs. A caller cannot assert its own risk score.
* **Advisory flags are inputs to gates, not to outcomes.** `critic_status: FAIL` and
  `injection_attempt: true` force a human gate; they do not change the risk arithmetic. The direction
  matters: an agent cannot lower a gate by asserting that it passed a critic.
* **The upstream agent's own guardrails compound.** WEBIS-SEARCH quarantines instruction-bearing pages
  before they can become findings; this service additionally refuses malformed or unknown inputs. Two
  independent walls.

## 4. Evidence integrity

Append-only hash chain (`PZ-nnnnnn`), canonical JSON, `prev_hash` links, tamper detection at the first
divergent `seq`, no delete and no update operation. `replay` re-evaluates a stored decision's input
against the policy on disk and compares — the reproducibility check that makes the ledger meaningful
rather than decorative. The signature is a KMS stub; that is a stated limitation, not a hidden one
(`spec/04 §5`).

## 5. Data protection

The decision payload contains names and ownership percentages. It is **not** written to the ledger: the
record keeps `input_hash`, `input_fields` and the decision, and the case file is retained under SOP
§8.1. The engine is offline and stateless; OPA receives the input for the duration of an evaluation and
retains nothing.

## 6. What this agent must never do

| Never | Enforced by |
|---|---|
| Return a permissive outcome for an unknown input, factor or action | `INVALID_INPUT`; default-deny in authority |
| Allow a sanctions hit to be cleared by an agent | `const false` in the schema + prohibited action |
| Finalise a case automatically | `auto_final_allowed: const false` |
| Change a policy without a named human approval | `bundle.activate` signature + `PROPOSAL.status: DRAFT` |
| Produce a decision that cannot be reproduced | determinism + `replay` + archived bundle |
| Read the network, the clock, or the filesystem from a rule | restricted capability compile |
| Let a rule silently disappear | `opa test` count is asserted by the Python suite (35/35) |

## 7. Residual risks

1. **A wrong rule is still a rule.** Everything here proves the decision matches the policy; nothing
   proves the *policy* matches the regulation. That is what the clause-to-proposal loop and human
   approval are for, and it is why `SOP_CDD_v4.2_excerpt.md` is in the repository next to the Rego.
2. **The corpus is small.** 10 cases catch regressions, not coverage gaps. A production deployment
   should replay a labelled sample of *real* past cases on every activation.
3. **Threshold edits are easy to miss in review.** `data.json` is pinned and diffed, but a one-line
   change from `25` to `20` deserves the same scrutiny as a new rule — the manifest diff must be read,
   not skimmed.
4. **The KMS signature is a stub.**
