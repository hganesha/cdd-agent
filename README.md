# Three agents, one control loop

A complete, runnable definition of the KYC investigation stack: an agent that investigates, an agent
that fetches evidence, and a policy service that decides. Each is specified to the same depth —
system prompt, skills with explicit inputs/outputs/evidence writes, I/O contracts, evidence writes,
long-running behaviour, guardrails, evaluation — and each is actually executable and tested.

```
                 ┌──────────────────────────────────────────────────────────┐
   web / registry │  WEBIS-SEARCH  ·  external evidence                      │
   media / court  │  18 queries → 40 fetches → passages → citations          │
                 │  refuses, quarantines, redacts; resumes after a crash    │
                 └───────────────────────┬──────────────────────────────────┘
                                         │  evidence_pack.json  (claims + citations only)
                                         ▼
                 ┌──────────────────────────────────────────────────────────┐
   case file     │  CDD-INVESTIGATION  ·  findings, coverage, memo          │
   documents     │  100s of pages, checkpointed, coverage matrix, gates     │
                 └───────────────────────┬──────────────────────────────────┘
                                         │  facts (factors, ownership, screening, coverage)
                                         ▼
                 ┌──────────────────────────────────────────────────────────┐
                 │  AXIOM-POLICY  ·  OPA/Rego decision service              │
                 │  risk band → ownership → screening → coverage → gates    │
                 │  closed-enum outcome, human approval, hash-chained ledger│
                 └──────────────────────────────────────────────────────────┘
```

The split is the point: **the agents propose, the policy decides.** No model can clear a sanctions hit,
approve a case, or lower a risk band — those verbs are not in its vocabulary, and the Rego service will
not accept them even if they were.

## The three packages

| Package | What it is | Verify it |
|---|---|---|
| [`cdd-investigation-agent/`](cdd-investigation-agent/README.md) | Investigation agent over a case file: 100s of pages, coverage matrix, risk floors, EDD/RFI gates, durable `case_state.json`. Spec 00–07, 8 schemas, 2 test suites. | `cd cdd-investigation-agent && make demo && make test` |
| [`policy-agent/`](policy-agent/README.md) | **AXIOM-POLICY**: real Rego evaluated by a vendored OPA v1.21.3 under restricted capabilities. Risk banding with eight band floors, 25 % UBO threshold + control prong, five screening lists with a prohibition on agent clearance, coverage minimums, gate routing, three-valued action authority, bundle build/verify/activate, clause→proposal authoring. | `cd policy-agent && make check` → `PASS: 35/35`, `22/22`, `10/10` |
| [`external-search-agent/`](external-search-agent/README.md) | **WEBIS-SEARCH**: governed external search. Source tiers, deny-list, robots/paywall compliance, citation-or-nothing packs, independence measured by *origin* (syndication collapsing), same-name discrimination, PII redaction, quarantine of instruction-bearing pages, checkpoint/resume. | `cd external-search-agent && make check` → `54/54`, `100.0 %` quote precision |

Everything runs offline: no network calls, no external services. `policy-agent/bin/opa` is vendored so
the policy service is genuinely runnable.

## What "complete definition" means here

For each agent, the same eight documents, in the same order, built from the CDD agent's shape:

```
spec/00-agent-definition.md      what it is, what it is not, division of labour, design positions
spec/01-system-prompt.md         the prompt's place in the control stack, clause by clause
spec/02-skills.md                every skill: inputs · tools · outputs · evidence writes · failure · tests
spec/03-io-contracts.md          the JSON Schemas and the guarantees they enforce
spec/04-evidence-writes.md       what is written, when, keyed how, how tampering is detected
spec/05-long-running-execution.md  checkpointing, resume guarantees, budgets, scale, failure modes
spec/06-guardrails.md            untrusted content, prohibitions, change control, residual risks
spec/07-evaluation.md            metrics, ground truth, SOP coverage, what the suite cannot tell you
```

plus `prompts/` (hash-pinned), `schemas/`, `config/`, a reference implementation, an executable test
suite, `Makefile` and `README.md`.

## Numbers that are asserted, not claimed

| Claim | Where it is proved |
|---|---|
| The policy compiles with no network, clock or filesystem | `test_policy_compiles_under_restricted_capabilities` injects `http.send` into a copy → compile fails |
| Two independent risk engines agree | `test_python_and_opa_risk_engines_agree` — Rego vs the CDD agent's Python model, ±0.01 across 8 factor combinations |
| An agent cannot clear a sanctions hit | `agent_may_clear_sanctions_hit: const false` in the schema + `sanctions.clear` in `prohibited_actions` |
| A policy change cannot self-activate | `activate()` requires `approver` + `ticket`; `PROPOSAL.status` is `const DRAFT` |
| Every quotation re-derives from its source | `make verify` → `quote_precision: 100.0` over the pack |
| Two outlets reprinting one wire are one source | `TestSKS6Triangulation` — `corroboration_count: 1`, with a note |
| A page that instructs the agent cannot influence it | `TestSKS9Quarantine` on an **allowed** tier-3 domain: quarantined, `status: PARTIAL`, zero claims |
| A crashed search resumes to the same answer | `TestSKS11Resumption` — interrupted + resumed `==` uninterrupted |
| A pinned corpus outcome cannot drift | `make corpus` → `10/10 cases match the pinned baseline`, non-zero exit on change |
| Decisions are reproducible from the ledger | `make replay` → `reproduced: true`, `differences: []` |

## The case that ties them together

`CASE-4831` — Meridian Trade Holdings Ltd, EDD, PEP exposure, 45 % of ownership unresolved, coverage at
16.7 %:

* **Search** returns four cited claims: FCA final notice `HIGH` (2 independent origins), negligent-not-
  dishonest `MEDIUM`, administration `LOW` *with a syndication note* (two outlets, one wire), customs
  inquiry `LOW` — plus three namesake exclusions, one quarantined injection, one aggregator allegation
  recorded as circulating-only, and a paywalled source declared as a gap.
* **Investigation** turns those into findings, computes a residual risk of **37.95** and a coverage
  matrix that cannot reach the EDD minimum.
* **Policy** returns `BLOCK`: `score 37.95`, `band HIGH` because a band floor fired on PEP exposure,
  `weighted_score_band LOW`, route `EDD_UNIT`, obligations `CLOSE_UBO_GAP` / `COMPLETE_SCREENING` /
  `HUMAN_EDD_APPROVAL`, `auto_final_allowed: false` — recorded as `PZ-nnnnnn`, reproducible by
  `make replay`.

Run each in one command:

```bash
cd external-search-agent && make demo
cd ../cdd-investigation-agent && make demo
cd ../policy-agent && make decide
```
