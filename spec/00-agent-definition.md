# CDD Investigation Agent — Master Definition

**Agent codename:** `ATLAS-CDD`
**Agent class:** Agentic, long-running, tool-using, human-supervised investigation worker
**Owning function:** Financial Crime Compliance (FCC) / KYC Operations
**Definition version:** 1.0.0
**Status:** Implementation-ready specification
**Primary regulatory anchor:** US FinCEN Customer Due Diligence Rule (31 CFR 1010.230) + FFIEC BSA/AML Manual; FATF Rec. 10 & 24; configurable for EU AMLD5/6 and UK MLR 2017

---

## 1. Purpose

`ATLAS-CDD` performs end-to-end **Customer Due Diligence (CDD) investigations** for individual and legal-entity customers. Given a case intake record and a document bundle that may run to **hundreds of pages** across PDFs, scans, spreadsheets, images and structured feeds, the agent:

1. Ingests, classifies and quality-scores every document.
2. Extracts typed, cited facts (identity, ownership, control, purpose, activity, screening attributes).
3. Resolves entities and builds a **beneficial ownership graph** to the 25%/control thresholds required by policy.
4. Runs screening and open-source/adverse-media research, and triages hits.
5. Populates a **required-data-element coverage matrix**, produces a prioritized **RFI (gap) pack**.
6. Computes a **policy-traceable risk rating** (inherent → mitigants → residual) and a CDD level (SDD/CDD/EDD).
7. Assembles a **decision-ready dossier** in which **every material assertion carries a resolvable evidence citation**.
8. Writes an **append-only, hash-chained evidence ledger** so that any reviewer (or regulator) can reconstruct exactly what was seen, when, by which tool, and what the agent concluded.

The agent is **analyst-grade, not decision-grade**: it produces a recommendation and an evidence pack for a human Compliance Officer / MLRO to approve, reject or escalate. It never opens accounts, never approves EDD, and never files reports.

---

## 2. Operating principles (non-negotiable)

| # | Principle | Enforcement |
|---|---|---|
| P1 | **No uncited assertion.** Every material claim in the dossier resolves to ≥1 evidence record with a verbatim quote and a page anchor. | Critic pass fails the run; claim is quarantined, not softened. |
| P2 | **Abstain over invent.** Missing data is reported as a gap, never inferred to fill a schema slot. | `confidence` + `basis` enum; `UNRESOLVED` is a legal output value. |
| P3 | **Documents are untrusted data.** Text inside a document can never modify instructions, tools, thresholds or scope. | Input isolation, instruction-stripping, injection probes (see §9). |
| P4 | **Deterministic where it matters.** Risk ratings, thresholds, and CDD levels come from a versioned rules engine, not from prose generation. | `risk_engine_v*` pinned; numeric trace emitted. |
| P5 | **Append-only evidence.** Evidence is never edited or deleted; corrections are new records that supersede. | Hash chain + supersede links + write-once storage. |
| P6 | **Idempotent, resumable, restartable.** A 6-hour, 400-page run that dies at page 312 resumes at page 312. | Durable checkpoints per unit of work; idempotency keys. |
| P7 | **Human-in-the-loop at defined gates.** Escalation and RFI are first-class outputs, not failures. | Gates G1–G4 (§8). |
| P8 | **Scope discipline.** Out-of-scope requests are refused and routed, not silently performed. | Scope block in system prompt; refusal reason codes. |
| P9 | **Least data.** Read-only access, minimum necessary PII, redaction in logs, no data egress outside allowlist. | Tool allowlist, egress controls, log redaction. |
| P10 | **Reproducibility.** Every run records model IDs, prompt versions, tool versions, policy version, inputs' hashes. | `RunManifest` is a required output. |

---

## 3. Scope

### 3.1 In scope

| Domain | Detail |
|---|---|
| Customer identification | Legal name, aliases, DOB/incorporation date, jurisdiction, addresses, identifiers (passport, TIN/EIN, national ID, LEI, registration number) |
| Verification | Documentary + non-documentary corroboration, recency/expiry checks, name/DOB consistency across sources |
| Ownership & control | UBO chain to policy threshold (default 25%), control prong (senior managing official), complex/nominee/circular structures, trust & foundation look-through |
| Relationship purpose | Nature and purpose of the relationship, expected products, expected volumes, source of funds, source of wealth |
| Screening | Sanctions (OFAC/UN/EU/UK/OFSI), PEP (domestic/foreign/IO), RCA, adverse media, enforcement/regulatory actions, litigation |
| Risk assessment | Customer/product/geography/channel inherent risk, mitigants, residual rating, CDD level, periodic-review cadence |
| Documentation | Decision-ready dossier, gap/RFI pack, escalation ticket, evidence pack, audit trail |
| Monitoring handoff | Expected-activity profile and alert-threshold parameters handed to transaction monitoring |

### 3.2 Explicitly out of scope (must refuse + route)

- Approving, rejecting or conditionally onboarding a customer; setting final risk rating.
- EDD *approval*, PEP committee sign-off, or risk-appetite exceptions.
- Filing or drafting SAR/STR, CTR, or any regulatory report.
- Sanctions **adjudication** — the agent triages and refers; it does not clear a true hit.
- Legal advice, tax advice, CRS/FATCA classification decisions, credit decisions.
- Any action on a live account (freeze, close, restrict, block payments).
- Covert investigation of the customer or contacting third parties without an approved RFI channel.
- Inferring protected characteristics (race, religion, ethnicity, health, sexual orientation, political affiliation beyond PEP status) or using them as risk factors.

---

## 4. Agent architecture

```
                        ┌──────────────────────────────────────────┐
                        │  CASE INTAKE ADAPTER (schema validation)  │
                        └───────────────────┬──────────────────────┘
                                            ▼
                    ┌───────────────────────────────────────────────┐
                    │  ORCHESTRATOR  (durable state machine,         │
                    │  checkpointing, budgets, retries, stall watch) │
                    └───┬───────────┬───────────┬───────────┬───────┘
                        ▼           ▼           ▼           ▼
                 ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
                 │ INGEST   │ │ EXTRACT  │ │  LINK /  │ │ SCREEN & │
                 │ SK-01/02 │ │ SK-03    │ │  UBO     │ │ OSINT    │
                 │          │ │ SK-04    │ │ SK-05/06 │ │ SK-07/08 │
                 └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
                      └────────────┴────────────┴────────────┘
                                            ▼
                              ┌───────────────────────────────┐
                              │  ANALYSE & RATE               │
                              │  SK-09 activity, SK-10 risk   │
                              │  SK-11 gaps/RFI, SK-12 memo   │
                              └──────────────┬────────────────┘
                                            ▼
                              ┌───────────────────────────────┐
                              │  CRITIC / QA  (SK-13)         │
                              │  citation audit, contradiction│
                              │  policy conformance, injection│
                              └──────────────┬────────────────┘
                                            ▼
                              ┌───────────────────────────────┐
                              │  HUMAN GATES G1–G4  (SK-14)   │
                              └───────────────────────────────┘

  CROSS-CUTTING: Evidence Ledger (append-only, hash-chained) · RunManifest ·
                 Policy Engine · Tool Gateway (allowlist, rate limits, audit)
```

**Substrate requirements:** durable workflow engine (Temporal / Step Functions / Dagster-style), object store for documents and evidence packs, append-only/immutable evidence store with WORM retention, vector + keyword index over page-level chunks, relational store for case/run/finding state, secrets-isolated screening connectors, human task queue.

---

## 5. Inputs

### 5.1 Required inputs

| ID | Input | Format | Notes |
|---|---|---|---|
| `IN-01` | Case intake record | JSON (`case-input.schema.json`) | Case ID, customer type, relationship type, products, jurisdictions, requested CDD level, SLA, jurisdiction profile, prior-case links |
| `IN-02` | Identity attestation / application data | JSON | What the customer *claims* — treated as claim, never as evidence |
| `IN-03` | Document bundle | PDF, DOCX, XLSX, CSV, JPG/PNG/TIFF, EML/MSG | 1–500 files, 1–2,000 pages total; per-file ≤ 200 MB |
| `IN-04` | Policy & threshold config | YAML (`config/policy.yaml`), versioned | UBO threshold, risk weights, decision matrix, retention, jurisdiction risk tiers |
| `IN-05` | Screening connector credentials/scopes | Secret ref (never inline) | Read-only screening/list APIs; no write scopes |
| `IN-06` | Optional: internal core-banking / KYC system extract | JSON/CSV, read-only | Prior KYC record, transaction summary, existing relationships |
| `IN-07` | Optional: corporate registry data | JSON | SoS filings, officers, share register, LEI record |
| `IN-08` | Human instructions / prior RFI responses | Text + attachments | Threaded into the case; treated as claim until corroborated |

### 5.2 Input validation rules

- Schema validation on `IN-01`; hard fail with `INVALID_CASE_INPUT` and field-level errors.
- Every document gets SHA-256; duplicates collapsed but **all source references retained**.
- Encrypted/password-protected files → `BLOCKED_INPUT` with a specific RFI, not a silent skip.
- Files > page/byte limits → `PARTIAL_INTAKE`, chunk-queued, and flagged in `RunManifest.limitations`.
- Non-English documents → language-tagged; translation is a *derived* artifact that never replaces the source quote in evidence.
- Any doc classified as a **synthetic/instruction-bearing artifact** (prompt injection, red-team marker) is quarantined and logged.

---

## 6. Outputs

All outputs are written to the case workspace and registered in the evidence ledger.

| ID | Output | Format | Consumer |
|---|---|---|---|
| `OUT-01` | `CDDInvestigationReport` (dossier) | JSON + rendered Markdown/PDF | Compliance Officer, MLRO |
| `OUT-02` | `EvidenceLedger` | Append-only JSONL, hash-chained | Audit, QA, regulator |
| `OUT-03` | `FindingsTable` | JSON array of `finding` records | Reviewer UI |
| `OUT-04` | `OwnershipGraph` | Nodes/edges + effective-% computation trace | Reviewer UI, EDD |
| `OUT-05` | `ScreeningResults` | Hit list with match scores + disposition *recommendation* | Screening ops, MLRO |
| `OUT-06` | `RiskRating` | Inherent/mitigant/residual + rule-by-rule trace | Compliance Officer |
| `OUT-07` | `GapPack` / `RFIPack` | Prioritized missing-item list + customer-ready request text | KYC analyst, customer |
| `OUT-08` | `EscalationTicket` | Reason code, severity, routed queue, summary | MLRO / EDD unit |
| `OUT-09` | `RunManifest` | Run IDs, models, prompt/tool/policy versions, input hashes, coverage, cost, timings, limitations | Audit, FinOps, QA |
| `OUT-10` | `CoverageMatrix` | Required-data-element × status × evidence ref | Reviewer UI |
| `OUT-11` | `MonitoringHandoff` | Expected-activity profile, thresholds, watchlist attributes | TM system |

### 6.1 Required report sections (`OUT-01`)

1. Case summary & recommendation (with explicit `CONFIDENCE` and `DECISION_AUTHORITY: HUMAN`)
2. Customer profile & identification (claimed vs verified, side-by-side)
3. Verification result per document (type, issuer, number, expiry, recency verdict, evidence refs)
4. Ownership & control analysis (graph, computed %s, control prong, unresolved links)
5. Nature & purpose of relationship, expected activity, source of funds/wealth
6. Screening & adverse media (queries run, lists, dates, hits, triage, residual concern)
7. Risk assessment (inherent, mitigants, residual, CDD level, review cadence, rule trace)
8. Data gaps & confidence (coverage %, gaps, RFI status, impact of each gap on the rating)
9. Red flags / suspicions observed (factual, no conclusion of criminality)
10. Recommendation, conditions, and next actions (RFI / EDD / escalate / periodic review)
11. Evidence pack index & methodology appendix (what was read, what was not, what failed)
12. Analyst sign-off block (human) + agent attestation block (versions, hashes)

### 6.2 Output quality contract

- 100% of `F-*` findings have `evidence_refs` non-empty.
- 100% of numeric risk inputs appear in `RiskRating.trace`.
- `RunManifest.coverage.pages_read / pages_total` reported; any shortfall itemized.
- Report is **internally consistent**: no field may differ between summary and detail without an explicit `supersedes` / `as_of` note.
- Output length target: 6–12 pages of substance; no filler; each statement either cited, flagged as gap, or flagged as judgment.

---

## 7. Skills & capabilities

Full contracts in [`02-skills.md`](02-skills.md). Summary:

| ID | Skill | One-line purpose |
|---|---|---|
| `SK-01` | Document Triage & Inventory | Classify, inventory, dedupe, quality-score, language-tag |
| `SK-02` | Layout-Aware Ingestion / OCR | Native text, OCR, tables, stamps, handwriting flags |
| `SK-03` | Typed Field Extraction | Schema-conformant facts with page-level citations |
| `SK-04` | Entity Resolution & Linking | Normalize names/IDs, merge aliases, link docs→persons→orgs |
| `SK-05` | Ownership Graph & UBO Computation | Build graph, compute effective %, control prong, detect gaps |
| `SK-06` | Corporate Registry & Document Corroboration | Cross-check filings, officers, share capital |
| `SK-07` | Screening & Hit Triage | Sanctions/PEP/RCA/enforcement; scored, disposition-recommended hits |
| `SK-08` | Adverse Media & Open-Source Research | Triage-tier, recency, dedupe, event timeline |
| `SK-09` | Activity Profile & SoF/SoW Consistency | Expected vs plausible vs observed; inconsistency flags |
| `SK-10` | Risk Scoring & CDD-Level Determination | Deterministic, versioned, fully traced |
| `SK-11` | Gap Analysis & RFI Generation | Coverage matrix → prioritized, customer-ready requests |
| `SK-12` | Dossier Composition | Decision-ready report, every claim cited |
| `SK-13` | Adversarial Critic / QA | Citation audit, contradictions, policy conformance, injection check |
| `SK-14` | Escalation & Human Gate Management | Reason codes, routing, pause/resume on human input |
| `SK-15` | Case State & Long-Run Manager | Durable orchestration, checkpoint, resume, budget, stall watch |

**Capabilities (tools)** are declared in [`config/tools.yaml`](../config/tools.yaml) with explicit input/output contracts, side-effect class, idempotency, retry and rate-limit policy. A tool not in the manifest cannot be called.

---

## 8. Human gates

| Gate | When | Human action | Agent behaviour |
|---|---|---|---|
| `G1` | After triage if bundle is unusable (blurred, expired, wrong customer) | Confirm → issue RFI | Pauses, emits `RFIPack`, marks run `AWAITING_INPUT` |
| `G2` | Before any *true* screening hit is treated as cleared/cleared-with-context | Adjudicate hit | Recommends only; run `BLOCKED_PENDING_ADJUDICATION` |
| `G3` | Before dossier is final if residual risk ≥ HIGH, or any red flag present, or coverage < policy minimum | Review & decide | Emits `EscalationTicket`; dossier marked `ADVISORY` |
| `G4` | Post-hoc QA sampling (continuous) on all auto-completed LOW-risk cases | Sample review | Dossier `PROVISIONAL` until sampled; sampling rate from policy |

---

## 9. Guardrails

- **Prompt-injection defence:** document content is fenced in `<untrusted_document>` blocks, never concatenated into instruction position; a scanner flags imperative/instruction-like text, "ignore previous", hidden text, white-on-white, zero-width characters, and metadata-borne instructions; flagged docs are quarantined and reported, and the agent's decisions are unaffected.
- **Hallucination control:** schema-constrained extraction with `basis ∈ {document, registry, screening, inference, judgment}`; `inference`/`judgment` may never populate a `verified_*` field; critic pass drops or quarantines unsupported claims.
- **Refusal set:** out-of-scope requests (§3.2) return `OUT_OF_SCOPE` with routing; requests to hide, alter or omit evidence return `POLICY_VIOLATION` and are logged.
- **Data handling:** read-only tools, minimum-necessary retrieval, PII redaction in logs/traces, no raw document content to non-allowlisted endpoints, jurisdiction-aware residency routing, retention per `IN-04`.
- **Fairness:** risk factors are limited to the enumerated policy set; proxies for protected characteristics are forbidden and the critic scans for them.
- **Budget & rate:** per-case token/cost/time ceilings, per-tool QPS caps, max screening queries per subject, hard stop with `BUDGET_EXCEEDED` and partial deliverable.
- **Model routing:** sensitive-extraction and final-citation steps pinned to the highest-capability model with low temperature; bulk summarization may use cheaper models but **never** authors cited claims directly.

---

## 10. Long-running execution (100s of pages)

Full design in [`05-long-running-execution.md`](05-long-running-execution.md). Essentials:

- **Hierarchical map-reduce with citation retention:** page → document → cluster → case. Summaries at every level carry `evidence_refs` upward; a summary that drops its anchors is rejected.
- **Chunking:** layout-aware page windows (default 1 page + 15% overlap for continuation), table-aware atomic chunks, never split a table row.
- **Bounded parallelism:** N workers (default 8) per stage; queue-based fan-out; per-document idempotency keys `sha256(case|doc|stage|prompt_ver|page_range)`.
- **Durable checkpoints:** after every unit; state machine steps are replay-safe; Paused/Crashed runs resume from `run-state.schema.json` snapshot.
- **Context economics:** retrieval over stuffing — the case-level synthesis prompt receives the **evidence index + top-k cited spans**, not the corpus. Token budget per step declared in config.
- **Progress & stalls:** heartbeat per unit, EWMA ETA, stall detector (no progress > `stall_timeout` → auto-retry → degrade → `PARTIAL_DELIVERY`).
- **Interim delivery:** at 70% SLA or coverage ≥ 80%, an interim dossier is emitted so the human is never blocked.
- **Reconciliation:** every claimed "read" page is verifiable in `RunManifest.page_ledger`; unreadable pages are enumerated with reasons.

---

## 11. Model & policy configuration

| Function | Class | Temp | Notes |
|---|---|---|---|
| Extraction (cited) | Frontier reasoning model | 0.0 | Schema-constrained, no free prose |
| UBO/ownership reasoning | Frontier reasoning model | 0.0 | Arithmetic delegated to `calc.ownership` |
| Adverse media research | Frontier + web tool | 0.2 | Allowlisted sources only |
| Risk narrative | Mid-tier | 0.2 | Writes prose *around* engine numbers |
| Critic/QA | Frontier, *different family* from author | 0.0 | Independent verification |
| Triage/classification | Small/fast | 0.0 | Cheap, high volume |

All model IDs, prompt hashes and policy versions recorded in `RunManifest`; prompts live in version control and are hash-pinned per run.

---

## 12. Evaluation & acceptance

See [`07-evaluation.md`](07-evaluation.md). Headline acceptance thresholds for production release:

| Metric | Threshold |
|---|---|
| Cited-claim precision (quote exists, supports claim, correct page) | ≥ 99.5% |
| Hallucinated material assertion rate | ≤ 0.1% |
| Required-field extraction F1 (per field, golden set) | ≥ 0.95 (IDs ≥ 0.99) |
| Coverage completeness on complete bundles | ≥ 98% |
| Screening recall on seeded true hits | 100% (zero-tolerance), FP triage precision ≥ 0.85 |
| Risk-rating agreement with senior analyst (±1 band) | ≥ 95%; exact ≥ 85% |
| Resumability (kill at random point, resume) | 100% of runs complete with no duplicate evidence |
| Injection resistance (adversarial bundle suite) | 100% no instruction compliance |
| Median human review time saved | ≥ 60% vs manual baseline |

---

## 13. Failure modes & required behaviour

| Failure | Detection | Behaviour |
|---|---|---|
| Document unreadable / OCR confidence < 0.6 | Quality score | Gap + RFI, no guessing |
| Conflicting identifiers across documents | `SK-04` contradiction detector | Both recorded, contradiction finding, escalate |
| Ownership chain breaks below threshold | `SK-05` graph closure check | `UNRESOLVED_UBO` finding + targeted RFI |
| Screening connector down | Tool error + retries exhausted | Run `PARTIAL`, dossier marked `SCREENING_INCOMPLETE`, gate G3 forced |
| Policy version changed mid-run | Version check at step entry | Pin to run-start version; note in manifest; never mix |
| Prompt injection detected | Scanner | Quarantine doc, log, continue with other evidence |
| Budget/time exhausted | Budget meter | `PARTIAL_DELIVERY` with explicit limitations |
| Critic finds ≥1 unsupported material claim after 2 repair loops | Critic gate | Quarantine claim, downgrade confidence, force human gate |

---

## 14. Package contents

```
spec/00-agent-definition.md      ← this document
spec/01-system-prompt.md         ← production system prompt (canonical)
spec/02-skills.md                ← 15 skill contracts (I/O/evidence writes)
spec/03-io-contracts.md          ← input & output field-level contracts
spec/04-evidence-writes.md       ← evidence ledger design + write map
spec/05-long-running-execution.md← orchestration, checkpointing, resumability
spec/06-guardrails.md            ← safety, injection, fairness, data handling
spec/07-evaluation.md            ← eval suite, metrics, acceptance, red-team
prompts/system.md                ← drop-in system prompt
prompts/subsystem-prompts.md     ← per-skill prompts (extraction, UBO, media, critic…)
schemas/*.schema.json            ← machine-readable contracts
config/tools.yaml                ← tool gateway manifest
config/policy.yaml               ← thresholds, weights, decision matrix
config/model-routing.yaml        ← model/prompt/version pinning
src/cdd_agent/*                  ← reference implementation (runnable, deterministic)
data/case_4831/*                 ← synthetic demo case: 23 documents / 300 pages
tests/                           ← test_resumability.py (9) · test_contracts.py (schema validation)
README.md · Makefile             ← orientation and one-command demo
out/                             ← generated run artifacts (dossier, ledger, findings, manifest)
```

The reference implementation is not a mock-up of the design: it executes the stage machine, writes
the same evidence records the specification mandates, and is gated by tests that assert the
resumability, citation and guardrail properties claimed in §9, §10 and §13.
