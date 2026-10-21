# Long-Running Execution Design — 100s of Pages, Hours of Work

Target envelope: **1–500 documents · 2,000 pages · up to 6 hours wall clock · up to ~$45 model spend ·
crash-resumable · progress-visible · never silently partial.**

---

## 1. Why naive approaches fail

| Failure | What happens at 300 pages |
|---|---|
| Single mega-prompt | Context blown; middle-of-corpus facts lost; cost quadratic; one API error loses everything |
| Summarise-then-forget | Citations dissolve; the dossier cannot cite what the summariser dropped |
| Sequential single-threaded extraction | 300 pages × 40 s = 3.3 h before screening even starts |
| Retry from scratch | One transient error at page 290 costs the whole run |
| "The model read it" | No page ledger → no way to prove coverage or find the gap |

The design below fixes each one.

---

## 2. Shape: hierarchical map-reduce **with citation retention**

```
LEVEL 0  PAGE       one PageContent per page (text + tables + bbox + OCR conf)
                      │  map: SK-02, SK-03 (per page / per doc)
LEVEL 1  DOCUMENT   ExtractedFact[] per document, each with page+span+quote
                      │  reduce: reconcile within doc → DocumentDigest (facts + anchors)
LEVEL 2  CLUSTER    grouped by evidence purpose (identity / ownership / financials /
                      screening-relevant / correspondence), typically 4–12 clusters
                      │  reduce: ClusterDigest — must carry evidence_ids, not prose only
LEVEL 3  CASE       SK-05 graph, SK-09 activity, SK-10 risk, SK-11 gaps
                      │  retrieval over the full evidence index, not the corpus
LEVEL 4  SYNTHESIS  SK-12 dossier cites from the evidence index; SK-13 re-verifies spans
```

**Non-negotiable reduce rule:** a digest at level *n* carries `evidence_ids` and `key_spans`
upward. SK-13 rejects any digest whose claims lack resolvable evidence ids. Summaries are therefore
*indexes*, never substitutes for the underlying records.

**Retrieval, not stuffing:** at levels 3–4 the model receives (a) the typed evidence index
(~200–800 compact records, not 2,000 pages), (b) top-k spans retrieved per question, (c) the
structured facts. A 300-page case fits in the working context as an index; the corpus never does.

---

## 3. Chunking policy

| Content | Unit | Rule |
|---|---|---|
| Native-text pages | 1 page | Overlap 15% of trailing text for sentences continuing across pages |
| Scanned pages | 1 page image + OCR text | Page is atomic — bounding boxes must not cross pages |
| Tables | table-atomic | Whole table in one unit even if it spans pages; never split a row |
| Legal documents (trust deeds, AoA) | clause/section | Chunk on headings; keep definitions section attached to any clause referencing it |
| Spreadsheets | sheet + row-block (≤200 rows) | Header row repeated; formulas evaluated, not read |
| Email threads | message | Conversation ordering preserved; attachment cross-referenced |
| Multi-page forms | form | Fields kept with their labels |

Chunks are addressable: `chunk_id = f(doc_id, page_range, kind)`. Every extracted fact records its
`chunk_id` so a reviewer can jump to the exact unit.

---

## 4. Orchestration state machine

```
INTAKE → TRIAGE → EXTRACT → LINK → OWNERSHIP → SCREEN → ACTIVITY → RATE → GAP → COMPOSE → CRITIQUE → DELIVER
   │        │         │        │        │         │          │        │      │        │          │         │
   └────────┴─────────┴────────┴────────┴─────────┴──────────┴────────┴──────┴────────┴──────────┴─────────┘
                        each transition = durable checkpoint (ckpt-NNNNNN)

Side states: AWAITING_HUMAN (G1..G4) · PAUSED (budget/SLA) · PARTIAL_DELIVERY · FAILED_RETRYABLE
```

`RunState` (persisted after **every** unit, `schemas/run-state.schema.json`):

```jsonc
{
  "run_id": "RUN-4831-20261020T141233Z",
  "case_id": "CASE-4831",
  "stage": "EXTRACT",
  "stage_entered_at": "2026-10-20T14:41:10Z",
  "checkpoint": "ckpt-000187",
  "completed_units": ["DOC-4831-001:SK-03", "DOC-4831-002:SK-03", "..."],
  "in_flight": [{"unit": "DOC-4831-041:SK-03", "worker": "w3", "attempt": 1, "started": "..."}],
  "dead_letter": [{"unit": "DOC-4831-033:SK-02", "error": "OCR_TIMEOUT", "attempts": 3}],
  "page_ledger": {"read": 214, "skipped_dupe": 12, "illegible": 3, "pending": 89},
  "budget_used": {"usd": 12.88, "tokens": 1904221, "wallclock_s": 3811},
  "counters": {"facts": 411, "evidence": 688, "findings": 9, "gaps": 5},
  "idempotency_keys": "bucketed store, key=sha256(case|doc|stage|prompt_ver|range)",
  "next_actions": ["fan_out:DOC-4831-042..047:SK-03", "then:LINK"],
  "notes": []
}
```

**Checkpoint cadence:** after every document (SK-02/03), every graph recomputation (SK-05), every
connector call batch (SK-07/08), every stage boundary, and every 60 s of wall clock regardless.

---

## 5. Idempotency & exactly-once evidence

```
key = sha256(case_id | skill_id | unit_id | input_content_hash | prompt_version | tool_versions)
```

- Before executing a unit, check the key store. Hit → reuse the stored result and its `evidence_ids`;
  do not re-call the model, do not append duplicate evidence.
- `evidence_id` allocation is deterministic from the key for content-derived records, so a retried
  unit produces the *same* id rather than a twin.
- Write path: `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` + append-if-new. Ledger append is
  the commit point; state checkpoint is written **after** ledger append (so evidence is never ahead
  of state, and never lost).
- Replay test asserts: evidence set after resume ≡ evidence set after uninterrupted run, byte-identical
  modulo timestamps.

---

## 6. Concurrency, retries, backoff

```yaml
concurrency:
  page_units: 8            # OCR-bound
  doc_units: 6             # extraction-bound
  llm_calls: 4             # provider rate-limit aware
  external_connectors: 2   # per connector, contract-limit aware
retry:
  max_attempts: 4
  backoff: exponential_jitter   # 2s, 8s, 32s, 128s ± 30%
  retry_on: [429, 500, 502, 503, 504, TIMEOUT, PARSE_TRANSIENT]
  no_retry_on: [400, 401, 403, 404, SCHEMA_PERMANENT, POLICY_VIOLATION]
  dead_letter_after: 4 attempts → DLQ + gap + limitation note
circuit_breaker:
  per_connector: {error_rate: 0.5, window_s: 120, cooldown_s: 300}
  open → mark dependent section SCREENING_INCOMPLETE, force G3, continue other work
```

**Graceful degradation ladder** (never a hard stop if partial value is possible):
`full pipeline → skip OCR for native-text docs → reduce media research depth → single-pass
extraction without reconciliation → interim dossier with limitations`.

---

## 7. Progress, ETA, and stall detection

- Heartbeat per unit (default 10 s). `SK-15` emits `ProgressEvent` to the case UI and the human task
  queue.
- ETA via EWMA of per-unit duration by stage, weighted by remaining unit counts, with a wide band
  (±35%) that narrows as the run progresses. Report stages, not percentages alone:
  *"EXTRACT 214/318 pages · SCREEN queued · est. 1 h 20 m remaining"*.
- **Stall detector:** no heartbeat for `stall_timeout` (default 180 s) → mark unit suspect → cancel &
  requeue → after 2 requeues, dead-letter it, record a gap, continue.
- **SLA guard:** at `sla.deadline − 30%` of remaining budget, or coverage ≥ 80%, emit an **interim
  dossier** (`status: PARTIAL`, `limitations` filled). The human is never blocked waiting for a
  perfect run.

---

## 8. Budget enforcement

```yaml
budget:
  max_usd: 45.0
  max_tokens: 6_000_000
  max_wallclock_s: 21600
  soft_stop_at: 0.8          # stop expanding scope, finish in-flight
  hard_stop_at: 1.0          # stop, deliver PARTIAL
  per_stage_caps:            # prevents one stage eating the case
    SK-08: {usd: 8.0}        # media research is the usual runaway
    SK-12: {usd: 4.0}
```

On soft stop: no new searches, no new document intake; finish synthesis with what exists.
On hard stop: `PARTIAL_DELIVERY`, `limitations: ["Stopped at budget cap; SK-08 completed 62% of
subject queries"]`, gate G3 forced.

---

## 9. Context management at scale

| Technique | Effect |
|---|---|
| Evidence index as the working set | 800 records ≈ 60–90k tokens; fits comfortably |
| Top-k span retrieval per question | Only cited text enters synthesis |
| Structured facts over prose digests | Cheaper, machine-checkable, contradiction-detectable |
| Prompt caching on the pinned system prompt + policy block | 30–60% input-token saving |
| Sectioned composition | Write the dossier section-by-section with its own retrieval; assemble at the end |
| Numeric work in tools | Percentages, ratios, ages, day-counts never computed by the model |
| Cross-document reconciliation on the fact table | Contradictions found by clause, not by memory |

**Hard limits with declared behaviour:** if the evidence index exceeds `index_token_cap` (default
120k), switch to scored retrieval + cluster digests and record `INDEX_REDUCED` in `RunManifest`
so the reviewer knows the synthesis worked from a reduced index.

---

## 10. Ordering strategy (get the decision-critical facts first)

1. **Triage everything** (cheap, fast) → know what you have.
2. **Prioritised extraction queue:**
   `P0` identity/registration docs (certificate of incorporation, passport, registry extract)
   `P1` ownership docs (share register, UBO declaration, org chart, trust deed)
   `P2` screening-relevant identity attributes for all parties → **start screening as soon as P0/P1
   attributes exist**, in parallel with bulk extraction
   `P3` financials, SoF/SoW, correspondence, misc.
3. **Early-warning rule:** if a sanctions TRUE/STRONG hit appears at any point, pause non-essential
   extraction, emit the escalation immediately (gate G2). Do not spend 3 hours finishing a dossier
   for a case that must go to the MLRO now.
4. **Two-pass extraction** only where policy requires it (identity + ownership): cheap pass to
   locate, precise pass to extract cited values.

---

## 11. Resumability contract (tested, not asserted)

| Test | Assertion |
|---|---|
| Kill at random 20 points across 300-page run | Each resumes and completes; no duplicate evidence; page ledger identical |
| Connector outage mid-screening | Run completes `PARTIAL`, `SCREENING_INCOMPLETE` set, G3 forced |
| Budget exhausted at 60% | `PARTIAL_DELIVERY` with explicit limitations, no fabricated sections |
| Human gate pause for 48 h | Run resumes from G-state with no loss; human decision recorded as W8 |
| Policy version changed mid-run | Run completes on pinned version; new version noted, never mixed |
| Same case re-run | Idempotent evidence; new run id; report regenerated with corrections noted |
| Ledger tamper injected | `chain_verify` fails; delivery blocked; incident raised |

---

## 12. Deployment topology

```
┌──────────────┐   ┌──────────────────┐   ┌────────────────────┐
│ API / intake │──▶│ Workflow engine  │──▶│ Worker pool (auto- │
│ (validated)  │   │ (Temporal/SFn)   │   │ scale 2–40)        │
└──────────────┘   │ stages, timers,  │   └─────────┬──────────┘
                   │ retries, human   │             │
                   │ tasks, budgets   │◀────────────┘
                   └────────┬─────────┘
                            ▼
     ┌──────────────┬───────────────┬──────────────┬───────────────┐
     │ Object store │ Evidence      │ Case/run DB  │ Vector+BM25   │
     │ (docs, WORM) │ ledger (WORM) │ (Postgres)   │ index         │
     └──────────────┴───────────────┴──────────────┴───────────────┘
                            │
     ┌──────────────┬───────┴───────┬──────────────┐
     │ Screening    │ Registry APIs │ OCR/VLM svc  │ Human task UI │
     │ connectors   │ (read-only)   │ (isolated)   │ + review app  │
     └──────────────┴───────────────┴──────────────┴───────────────┘
```

Operational SLOs: p95 dossier for a 100-page case ≤ 45 min; 300-page ≤ 4 h; availability 99.5%;
workflow durability 100% (no lost runs); DLQ age alert at 15 min; ledger append p99 < 50 ms.

---

## 13. Cost model (typical 300-page corporate case)

| Stage | Calls | Est. cost |
|---|---|---|
| Triage + OCR | 300 pages | $1.50 |
| Extraction (2-pass, 60 fact-bearing pages) | ~180 | $6.00 |
| Digests + reconciliation | ~40 | $3.00 |
| Screening + media research | ~30 queries + 25 fetches | $9.00 |
| Ownership/risk (deterministic + narrative) | ~15 | $2.50 |
| Dossier + critic (2 loops) | ~12 | $7.00 |
| **Total** | | **~$29** (cap $45) |

Human baseline for the same case: 6–9 analyst hours. Target saving ≥ 60% with higher citation
completeness than manual work.
