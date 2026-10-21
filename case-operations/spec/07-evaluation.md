# 07 · Evaluation

Two questions: does it work, and how would we know if it stopped working? The first is answered by 53
acceptance tests and two corpus cases. The second is answered by a regression process in which every
number that matters is pinned, and a change to any of them has to be declared.

## 1. Suites

`python3 tests/test_ops_agents.py` — 53 tests, 3 seconds, no network.

| suite | tests | what it establishes |
|---|---|---|
| `TestProfile` | 12 | schema conformance; all twenty factors; every factor evidenced; arithmetic re-derives; coverage is arithmetic; ownership gap reported not filled; unavailable list → incomplete screening; geography uses the policy lists; clean case is clean; unknown factor raises; determinism and no duplicate evidence; delta detection; retries visible |
| `TestVerify` | 12 | clean bundle PASSes; tampered arithmetic; broken chain; tipping-off; PII; injection; staleness; self-approval; agent-named approver; obligation without action; policy replay mismatch; empty bundle fails |
| `TestOutreach` | 8 | draft never exceeds `READY_TO_SEND`; questions map to requirements; internal obligations never reach the customer; internal-only case produces no letter; round budget refused; ten-business-day deadline; send requires a named human with a TTL; assessment outcomes and next actions; no prohibited language or PII |
| `TestDecide` | 11 | schema conformance and never-final; envelope projected verbatim; recommendation admissible for band; actions cover obligations exactly; blocked obligation explains itself; critic failure returns the case for rework; downgrade instead of forcing; undeclared factor stops the run; narrative quotes the policy and refuses finalisation; narrative invents no reason; policy unavailable stops; diff reports what changed |
| `TestIntegration` | 7 | both corpus cases end to end; chain valid and append-only; kill/resume equality; budget → PARTIAL; atomic checkpoint; one ledger for two cases without collision |
| `scripts/check_contracts.py` | 25 checks | schemas valid; every stored artefact conforms; prompts pinned; chain valid; record types closed; evidence ids unique; policy service answers; disposition never admits agent finalisation; `case.finalise` denied |

## 2. Corpus cases

Both are run by `make run` and asserted end to end by `TestIntegration`.

### CASE-4831 · Meridian Trade Holdings Ltd — should block

| signal | value | source |
|---|---|---|
| PEP | director, former deputy minister of energy (VE) | `/c360/related-parties` |
| ownership | 45% through an undeclared nominee | `/c360/ownership` |
| turnover | 2.4× declared, +143.3% deviation | `/tx/summary` + `/tx/flows` |
| concentration | HHI 0.45, largest month 67% of inflow | `/tx/flows` |
| counterparty | CP-77 incorporated 5 months ago, no employees, no premises | `/counterparties` |
| geography | YE nexus (grey list), AE operation | `/lists/jurisdictions` |
| coverage | 30.0% — RDE-03, RDE-04, RDE-07, RDE-08 blocking | `/docs/documents` |
| screening | EU_CFSP unavailable → incomplete | `/lists/jurisdictions` |
| internal risk | B-, watchlisted | `/risk/ratings` |
| **expected** | `BLOCK`, band `HIGH` (weighted `MEDIUM`, floor applied), route `EDD_UNIT`, recommendation `ESCALATE_EDD`, state `PENDING_APPROVAL` | policy |

### CASE-4902 · Nordwind Logistics GmbH — should proceed with conditions

Clean control chain, all blocking requirements satisfied, coverage 100%, no adverse media, rating A.
`PERMIT_WITH_CONDITIONS`, band `LOW`, route `COMPLIANCE_OFFICER`, recommendation
`PROCEED_WITH_CONDITIONS`, one obligation (`COMPLETE_SCREENING`, because the EU list is unavailable),
no customer contact.

The pair matters more than either case: the same pipeline, the same code, opposite conclusions, and in
both cases no finalisation.

## 3. Properties, not just examples

These hold for every input, and are tested as properties:

| property | test |
|---|---|
| no artefact can carry an agent in `approver` | `test_agent_named_approver_is_caught`, schema `const` |
| no disposition can be finalised | schema `const false`, `test_disposition_matches_schema_and_is_never_final` |
| no recommendation outside the band matrix is presented | `test_recommendation_is_admissible_for_the_band` |
| re-running an agent adds no evidence | `test_same_input_same_profile_id_and_no_duplicate_evidence` |
| a resumed run equals an uninterrupted run | `test_kill_and_resume_yields_the_same_profile_without_new_evidence` |
| a budget stop yields `PARTIAL`, never a number | `test_budget_exhaustion_is_a_partial_not_a_fabrication` |
| every factor cites evidence that exists | `test_every_factor_is_evidenced` |
| the arithmetic in an artefact re-derives | `test_transaction_metrics_re_derive`, `test_coverage_is_arithmetic`, V-003 |

## 4. Regression process

1. **Numbers that matter are pinned.** Expected outcomes for both cases are asserted in
   `TestIntegration`; the policy's own corpus pins the policy side. A change that moves a case fails a
   test before it reaches a reviewer.
2. **A declared change touches the pin in the same commit**, with the reason — the policy pack does
   this in `policy-agent/corpus/expected.json`, and this package follows the same discipline for
   profiles and dispositions.
3. **Prompt changes re-pin.** `prompts/pins.json`; `make contracts` fails otherwise.
4. **A new factor is a policy change first.** The policy pack defines the vocabulary; the profiler
   maps to it. Adding a factor here without adding it there is a `KeyError`, which is the intended
   outcome.
5. **`make demo` is the smoke test** a reviewer runs before believing any claim in this document.

## 5. Add a case

1. Add fixtures under `mock/api/<case>/` with the same endpoint shapes (or add a customer block to
   the existing files keyed by id).
2. Add the case to `CASES` in `src/opsagents/cli.py` and to `config/ops.yaml`.
3. Run `make run CASE=<new>` and read the output. If the conclusion is surprising, the fixture is
   probably wrong, not the policy — check the policy decision first (`make decisions` in
   `policy-agent`).
4. Pin the expected outcome in `TestIntegration` with a one-line rationale.

## 6. Known limits — what these tests do not prove

| limitation | consequence |
|---|---|
| fixtures are synthetic | the pipeline is proven; the *risk judgements* on real data are not |
| the critic checks structure and consistency, not truth | a consistent lie that satisfies arithmetic and language checks passes V-003 and V-010 |
| models are not evaluated for prose quality | the narrative tests assert content requirements (outcome, reasons, refusal), not readability |
| single-process ledger sequencing | concurrent deployment needs sequence allocation (see `05` §7) |
| no longitudinal evaluation | one `as_of` per run; drift is detected between runs, not within one |
| outreach responses in tests are synthetic | the assessment logic is proven against constructed documents, not against real ones |

## 7. Definition of done for this package

* `make test` — 53/53.
* `make contracts` — 25 checks, all ok.
* `make demo` — both cases, crash, resume, ledger, contracts, in one command.
* Every skill in `02-skills.md` names an acceptance test that exists.
* Every guardrail in `06-guardrails.md` names an enforcement point that exists.
* No number in an agent's code that should be in `config/` or the policy pack.
* No path by which a disposition becomes final.
