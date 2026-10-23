# ATLAS-CDD — Customer Due Diligence Investigation Agent

A complete, implementation-ready definition of an agentic CDD investigator for a KYC function:
**system prompt, 15 skill contracts, inputs/outputs, evidence-write model, long-running execution
design, guardrails, evaluation plan — plus a runnable reference implementation** that works a
300-page case end to end, crashes, resumes, and produces a cited dossier.

```
Definition     spec/00-agent-definition.md          ← start here
Prompt         prompts/system.md                    ← drop-in, hash-pinned
Skills         spec/02-skills.md                    15 contracts (I/O/evidence writes)
Contracts      spec/03-io-contracts.md  schemas/    field-level + JSON Schema
Evidence       spec/04-evidence-writes.md           append-only hash-chained ledger
Long-running   spec/05-long-running-execution.md    checkpoint / resume / budget / degrade
Guardrails     spec/06-guardrails.md                injection, fairness, data handling
Evaluation     spec/07-evaluation.md                golden set, metrics, release gate
Synthetic set  evals/GOLDEN_SET.md                  260 cases with reproducible gold
Reference impl src/cdd_agent/                       runs the demo case offline
Demo case      data/case_4831/                      23 documents, 300 pages
```

---

## 60-second tour

The agent investigates a customer (individual, company, trust or partnership) across a document
bundle that may run to hundreds of pages, and produces a **decision-ready dossier in which every
material claim resolves to a quoted, page-anchored evidence record**.

It is deliberately **analyst-grade, not decision-grade**: it recommends, cites and escalates. A named
human Compliance Officer decides, and `decision_authority: HUMAN` is hard-coded into the output
contract.

The ten stages:

```
INVENTORY → EXTRACT → LINK → OWNERSHIP → SCREEN → ACTIVITY → RATE → GAP → COMPOSE → CRITIQUE → DELIVER
```

…with `SK-15` wrapping all of it for durable state, and the critic (`SK-13`) trying to break the
dossier before a regulator does.

---

## Run it

No API keys, no network, no installs beyond `pyyaml` + `jsonschema`.

### Compare with a Pi agent

The one-case pilot in `evals/compare.py` runs the existing reference implementation,
then optionally starts a Pi agent under the CDD instructions in `pi_cdd/AGENTS.md`.
Pi receives the same synthetic case, policy, and seeded screening responses as read-only
inputs in an isolated workspace. It does not see the reference output. The comparison
records elapsed time, provider-reported Pi tokens/cost, reference estimates, seeded
decision checks, and exact source-quote validity.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyyaml jsonschema
.venv/bin/python evals/compare.py --pi
# output: out/benchmark/comparison.json
```

Pi must be installed and configured with an OpenRouter API key. The default model is
`openai/gpt-4.1-mini`; pass `--model` to change it. This pilot is a **single synthetic
case**. The 260-case synthetic oracle is available in `evals/golden/` and described
in `evals/GOLDEN_SET.md`. Each case includes seeded screening and registry responses
plus a mock web corpus for the sibling external-search agent. A separate corpus
comparison runs five representative cases by default:

```bash
.venv/bin/python evals/compare_corpus.py --dry-run
.venv/bin/python evals/compare_corpus.py --pi
# output: out/corpus_benchmark/comparison.json
```

Use `--all --pi --resume` for the full set. The reference media stage now reads media
printouts from the input bundle, but other stages remain specific to `CASE-4831`.
The corpus runner rejects a dossier with the wrong customer or case before scoring.
Generalize the remaining stages before interpreting the corpus as a head-to-head
accuracy comparison. The reference's
USD/token fields are simulated workload estimates, while Pi's are provider-reported
usage. Pi's compact JSON answer does not replace the reference's schema-conformant
dossier, evidence ledger, resume behavior, or human gates.

```bash
cd src
python3 -m cdd_agent.cli run --clean        # full investigation (~1 s, deterministic harness)
python3 -m cdd_agent.cli verify             # independent hash-chain verification
```

Crash/resume demo:

```bash
python3 -m cdd_agent.cli run --clean --kill-at 8   # simulate a crash after 8 work units
python3 -m cdd_agent.cli resume                    # resume from the checkpoint
```

Budget-stop demo:

```bash
python3 -m cdd_agent.cli run --clean --budget 6     # hard stop → PARTIAL delivery, limitations filled
```

Tests (no pytest required):

```bash
python3 tests/test_resumability.py    # 9 tests: crash/resume equivalence, tamper detection, budget
python3 tests/test_contracts.py       # validates every artifact against its JSON Schema
```

### What a run produces (`out/`)

| File | What it is |
|---|---|
| `CDD_dossier_CASE-4831.md` | The 12-section decision-ready dossier, every claim cited `[EV-…]` |
| `report.json` | Machine-readable twin, conforms to `cdd-memo.schema.json` |
| `evidence_ledger.jsonl` | Append-only, hash-chained evidence (119 records on the demo case) |
| `findings.json` | Structured findings with severity, basis, confidence, evidence refs |
| `coverage_matrix.json` | 24 required data elements × status × evidence |
| `rfi_pack.json` | Prioritised, customer-ready requests for what is missing |
| `document_manifest.json` | Document inventory + full page ledger |
| `run_manifest.json` | Versions, input hashes, coverage, cost, limitations, chain head |
| `progress.jsonl`, `state/` | Progress events, checkpoints, durable fact store |

### The demo case (seeded on purpose)

Meridian Trade Holdings Ltd — BVI commodity trader, 23 documents, 300 pages. It contains:

- a **nominee shareholder holding 45%** with no disclosed principal → `UNRESOLVED_UBO`
- a **PEP match** on the principal beneficial owner → band floor, EDD, gate G3
- a **sanctions look-alike** that must *not* be cleared by the agent and must *not* inflate the rating
- an **adverse media event at allegation stage** (open inquiry, no charges) with a same-name exclusion
- **throughput annualising to 41.5× audited revenue** → activity inconsistency, no criminality claim
- a **prompt-injection attempt** in white-on-white text → quarantine, finding, no compliance
- **3 illegible scanned pages** → reported honestly, never guessed
- a **degraded screening connector** → `SCREENING_INCOMPLETE` + forced gate, not "no hits"

Result: residual **HIGH** (the weighted score alone would band LOW — policy floors apply), required
CDD **EDD** while the relationship was requested at CDD, coverage **81%** (17/24 satisfied, 5 partial,
0 missing), 3 decision-blocking gaps, recommendation **HOLD_FOR_RFI**, gates **G2 + G3**, 12 findings,
119 evidence records, critic PASS.

---

## The 15 skills

| ID | Skill | Emits |
|---|---|---|
| SK-01 | Document triage & inventory | W1 per document, run registration |
| SK-02 | Layout-aware ingestion / OCR | W2 per page used, quarantine on injection |
| SK-03 | Typed field extraction | W3 per material field, with verbatim span |
| SK-04 | Entity resolution & linking | W3 attributes, W6 contradictions |
| SK-05 | Ownership graph & UBO computation | W3 edges, W4 per computed %, W6 structural flags |
| SK-06 | Registry corroboration | W5 per query (incl. NOT_FOUND), W6 mismatches |
| SK-07 | Screening & hit triage | W5 per subject×list (incl. zero-hit), W6 triage |
| SK-08 | Adverse media research | W5 queries, W3 events, W6 exclusions |
| SK-09 | Activity & SoF/SoW consistency | W3 inputs, W4 ratios, W6 inconsistencies |
| SK-10 | Risk scoring & CDD level | W4 engine trace (deterministic, never an LLM) |
| SK-11 | Gap analysis & RFI | W6 gaps, W7 RFI artifact |
| SK-12 | Dossier composition | W7 dossier + JSON report |
| SK-13 | Adversarial critic / QA | W8 critique, W6 unrepaired defects |
| SK-14 | Escalation & human gates | W8 gate events with actor + timestamp |
| SK-15 | Case state & long-run manager | W1 run registration, W8 stage transitions |

Write classes (`W1`–`W9`) are defined in `spec/04-evidence-writes.md`; the rule that makes the whole
thing auditable is **"no query, no clearance"** — a zero-hit screening result still needs a
timestamped, dataset-versioned query record, because the query *is* the evidence.

---

## What the reference implementation proves

Each claim below is asserted by an executable test, not by prose:

| Claim | Test |
|---|---|
| Crash at any of 1/4/8/12 units and resume → identical page ledger, identical findings, zero duplicate evidence | `test_kill_and_resume_matches_clean_run`, `test_resume_at_multiple_points` |
| Every finding carries ≥1 resolvable evidence reference | `test_every_finding_is_cited` |
| All evidence write classes actually occur | `test_evidence_write_classes_present` |
| Injection is quarantined, reported, and never influences conclusions | `test_injection_quarantined_and_reported` |
| A WEAK sanctions look-alike neither clears nor escalates the rating | `test_sanctions_lookalike_not_cleared_or_inflated` |
| Budget exhaustion yields an honest PARTIAL with limitations | `test_budget_hard_stop_delivers_partial` |
| Tampering with the ledger is detected | `test_chain_tamper_is_detected` |
| Every artifact conforms to its JSON Schema | `tests/test_contracts.py` |

Resume correctness depends on the **durable fact store** (`out/state/case_state.json`): extracted
facts, findings and the page ledger are persisted per unit. A resume that rebuilds state from the
ledger alone would silently lose extracted facts — the harness caught exactly that bug during
development, which is why the fact store exists.

---

## Wiring it into production

The reference implementation is deterministic and model-free so it runs offline. Replace three
adapters; everything else — contracts, evidence writes, idempotency, state machine — stays.

```python
# 1. extraction adapter (src/cdd_agent/skills.py :: sk03_extract_document)
#    replace the regex FIELD_PATTERNS pass with a constrained-decoding LLM call
facts = llm.extract(page_ref=f"{doc_id}:{page}",
                    schema_id="extraction-result.schema.json",
                    prompt_version=PROMPTS["extract"])
# the contract it must honour: value + verbatim_quote + page + char_span + basis + confidence

# 2. connector adapters (sk06_registry / sk07_screening / sk08_media)
#    swap SEEDED_LISTS for real list providers; keep recording dataset_version + queried_at

# 3. composition + critic (sk12_compose / sk13_critic)
#    frontier model for compose, a *different model family* for the critic,
#    both reading retrieved spans rather than the corpus
```

Then keep, unchanged:

- the system prompt (`prompts/system.md`, hash-pinned in `RunManifest.prompt_hash`)
- the evidence ledger and its write classes
- the tool gateway manifest (`config/tools.yaml`) — anything not listed cannot be called
- the policy pack (`config/policy.yaml`) — all thresholds, weights, band floors, decision matrix
- the human gates

### Deployment checklist

1. Durable workflow engine (Temporal / Step Functions) for the stage machine; object store with WORM
   for documents and the ledger; Postgres for case/run state; page-level search index.
2. Screening and registry connectors behind an internal proxy with read-only scopes, rate limits and
   audit logging. No credential ever reaches a prompt.
3. Reviewer UI that renders each claim with its evidence record inline (quote + page crop + confidence).
4. Retention configured to policy (5 years default, 10 for EDD/MLRO-referred), deletion as a
   controlled lifecycle job — never a runtime capability.
5. Run the eval harness (`spec/07-evaluation.md`) against the 260-case golden set and clear the
   release gate before any production case.

---

## Deliberate design choices worth arguing about

- **Abstention is a first-class output.** `UNRESOLVED` is legal; a fabricated value is a P0 incident.
  Coverage below the policy minimum *blocks* a FINAL status rather than being footnoted.
- **The risk engine is not an LLM.** Scores are a pure function of (factor inputs, policy version).
  The model explains the trace; it may not produce it. Band floors exist because a single severe
  factor (PEP, opaque ownership, unexplained throughput) must not be averaged away by benign ones.
- **The critic runs on a different model family** from the composer, to avoid correlated blind spots.
- **Partial delivery is normal.** An interim dossier at 70% of SLA beats a perfect dossier that
  arrives after the human needed it. `PARTIAL` with filled-in limitations is a success state.
- **The agent never closes a case.** No code path leads from agent output to a closed case; every
  high-risk, gapped or hit-bearing case routes to a human gate with a decision-ready question.

---

## Repository layout

```
spec/            00 definition · 01 system prompt · 02 skills · 03 I/O contracts
                 04 evidence writes · 05 long-running · 06 guardrails · 07 evaluation
prompts/         system.md (drop-in) · subsystem-prompts.md (per-skill stage prompts)
schemas/         8 JSON Schemas (case input, evidence, manifest, finding, risk, coverage, memo, run state)
config/          tools.yaml (gateway manifest) · policy.yaml (thresholds/weights) · model-routing.yaml
src/cdd_agent/   evidence.py · policy.py · skills.py · orchestrator.py · cli.py · make_case.py
data/case_4831/  case.json · bundle.json · 23 document page files (300 pages)
tests/           test_resumability.py (9) · test_contracts.py (schema validation)
out/             run artifacts (regenerated by the CLI)
```

---

*Synthetic case data. No real customer information. The dossier format, evidence model and guardrails
are written against FinCEN CDD (31 CFR 1010.230), FFIEC BSA/AML guidance and FATF Rec. 10/24, and are
configurable to other regimes via the policy pack.*
