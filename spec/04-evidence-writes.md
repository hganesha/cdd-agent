# Evidence Writes — Ledger Design & Write Map

The evidence ledger is the agent's defensibility. If a reviewer cannot reconstruct a conclusion
from the ledger alone, the conclusion does not exist.

---

## 1. Properties

| Property | Implementation |
|---|---|
| **Append-only** | JSONL/Parquet append + object-lock (WORM). No UPDATE, no DELETE. Ever. |
| **Hash-chained** | `record_hash = sha256(canonical_json(record \ {record_hash, signature}) \|\| prev_hash)`. Any tamper breaks the chain from that point. |
| **Signed** | Per-run signing key (KMS); signature over `record_hash`. Verifiable offline. |
| **Attributable** | Every record names `created_by` = `{skill, tool, model_id, prompt_hash, run_id, actor}`. |
| **Anchored** | Content-bearing records carry `locator` = doc id + page + char span + bbox + verbatim quote. |
| **Sufficient** | A record alone must be enough to justify the claim it supports: quote, location, method, confidence. |
| **Correctable, not mutable** | Errors are fixed by a new record with `supersedes: EV-…` and `correction_reason`; the original stays. |
| **Retrievable** | Indexed by `case_id, evidence_id, finding_id, doc_id, entity_id, skill, record_type, created_at` for regulator-style queries. |
| **Retained** | Policy retention (default 5 years post-relationship end, configurable per jurisdiction; up to 10 for EDD). Deletion is a controlled, logged lifecycle event, not a runtime capability. |
| **Minimal** | Only the spans needed, never whole documents. PII minimised; special-category data never captured. |

---

## 2. `EvidenceRecord` schema

```jsonc
{
  "evidence_id": "EV-4831-000211",
  "case_id": "CASE-4831",
  "run_id": "RUN-4831-20261020T141233Z",
  "record_type": "OBSERVATION",         // see §3 write classes
  "seq": 211,                            // monotonic within case
  "created_at": "2026-10-20T16:11:02.481Z",
  "created_by": {
    "skill": "SK-05",
    "tool": "doc.read_page",
    "model_id": "extract-model-pinned-2026-09",
    "prompt_hash": "sha256:ab12…"
  },

  /* WHAT was observed */
  "subject": {"kind": "SHAREHOLDING", "entity_id": "ENT-4831-P03", "field": "ownership.percentage"},
  "value": {"raw": "450 ordinary shares of 1,000 issued (45%)", "normalized": "45.00", "datatype": "percent"},
  "verbatim_quote": "Sterling Nominees Limited — 450 ordinary shares (45.0%) — registered 12 Mar 2021",
  "translation": null,                   // if source non-English: translated string, source kept above
  "basis": "document",                   // document | registry | screening | inference | judgment
  "confidence": "HIGH",
  "extraction_method": "native_text",    // native_text | ocr | table | vlm | api | human

  /* WHERE it came from */
  "locator": {
    "doc_id": "DOC-4831-011",
    "doc_sha256": "3ad9…",
    "page": 7,
    "char_span": [1841, 1938],
    "bbox": [72, 410, 523, 442],
    "table": {"name": "Register of Members", "row": 3, "col": 2}
  },
  "source_meta": {"issuer": "BVI Registered Agent", "doc_date": "2021-03-12", "tier": 1},

  /* FOR EXTERNAL QUERIES (record_type = EXTERNAL_QUERY) */
  "query": {
    "connector": "sanctions.screen",
    "dataset": "OFAC_SDN", "dataset_version": "2026-10-18",
    "queried_at": "2026-10-20T15:02:11Z",
    "parameters_hash": "sha256:…", "response_ref": "s3://…/raw/scr_00912.json",
    "result_count": 1
  },

  /* FOR COMPUTATIONS (record_type = COMPUTATION) */
  "computation": {
    "calc_id": "calc.ownership@v1.2.0",
    "inputs": [{"evidence_id": "EV-4831-000211", "factor": "0.45", "path": "A"},
               {"evidence_id": "EV-4831-000203", "factor": "0.60", "path": "A"}],
    "operation": "sum(path_products)",
    "result": "27.00"
  },

  /* LINKAGE */
  "supports": ["FND-4831-0014"],          // findings this record supports
  "cited_by": ["RPT-4831-01#s4.2"],       // report locations citing it
  "supersedes": null,
  "superseded_by": null,
  "correction_reason": null,
  "quarantined": false,
  "quarantine_reason": null,

  /* INTEGRITY */
  "prev_hash": "sha256:91fe…",
  "record_hash": "sha256:7c02…",
  "signature": "kms:…"
}
```

---

## 3. Write classes — the complete write map

Every agent action that touches knowledge writes exactly one of these. If an action is not on this
list, it must not produce durable knowledge.

| Class | `record_type` | Written by | When | Content | Mutability |
|---|---|---|---|---|---|
| **W1** | `REGISTRATION` | SK-01, SK-15 | Run start; each document registered | Input hashes, doc hash, type, page count, source, versions | Append-only |
| **W2** | `OBSERVATION` | SK-02 | On reading a page whose content is used | Page text span, method, OCR confidence | Append-only |
| **W3** | `EXTRACTION` | SK-03, SK-04, SK-08 | On extraction of each material field/event | value + normalized + verbatim quote + locator | Append-only |
| **W4** | `COMPUTATION` | SK-05, SK-09, SK-10 | On every computed number | inputs (evidence ids + factors), operation, result, tool version | Append-only |
| **W5** | `EXTERNAL_QUERY` | SK-06, SK-07, SK-08 | **Every** external query, including zero-hit | connector, dataset + version, timestamp, params hash, raw response ref, result count | Append-only |
| **W6** | `FINDING` | SK-04…SK-13 | On identifying a material condition | finding object, severity, basis, evidence_refs, policy_refs | Append-only; status changes via new `STATUS_CHANGE` record |
| **W7** | `ARTIFACT` | SK-11, SK-12 | On producing a deliverable | artifact hash, URI, section list, citation count | Append-only |
| **W8** | `GATE` / `STATE` | SK-13, SK-14, SK-15 | Stage transition, critic result, human decision, pause/resume | who/when/what/on-what-evidence | Append-only |
| **W9** | `CORRECTION` | any | On fixing an error | `supersedes`, corrected value, reason, author | Append-only |

### 3.1 Mandatory write triggers (checklists used by SK-13)

**SK-01 must write:** one `W1` per document · one `W1`-class run-registration at start · one `W2`
(quarantine) per injection-flagged document.
**SK-02 must write:** `W2` for every page from which a fact is later extracted.
**SK-03 must write:** `W3` for every material field — names, dates, identifiers, addresses,
amounts, percentages, roles, jurisdictions.
**SK-04 must write:** `W3` per corroborating attribute used in a merge; `W6` per `PROBABLE_MATCH`
and per `CONTRADICTION`.
**SK-05 must write:** `W3` per shareholding/directorship edge; `W4` per effective-% computation and
per control-prong determination; `W6` for each structural red flag.
**SK-06 must write:** `W5` per registry query (including `NOT_FOUND`); `W6` per mismatch.
**SK-07 must write:** `W5` per (subject × list) query **even when zero hits** — the query is the
evidence; `W6` per hit triage disposition.
**SK-08 must write:** `W5` per search query; `W3` per retained event with tier + verification
status; `W6` per relevance exclusion decision.
**SK-09 must write:** `W3` per input fact; `W4` per ratio; `W6` per inconsistency.
**SK-10 must write:** `W4` for the engine call — factor inputs, weights, contributions, score,
band; `W6` for any unresolved factor.
**SK-11 must write:** `W6` per gap; `W7` for the RFI artifact.
**SK-12 must write:** `W7` for the dossier with citation index.
**SK-13 must write:** `W8` critique result; `W6` per unrepaired defect.
**SK-14 must write:** `W8` per gate event, including the human decision verbatim and the actor id.
**SK-15 must write:** `W1` run registration; `W8` per stage transition; never a synthetic measurement.

---

## 4. The "no query, no clearance" rule

A zero-hit screening result is **not** an absence of evidence — it is evidence that a query was run.
Therefore every subject × list pair produces a `W5` record containing `dataset_version` and
`queried_at`. A dossier that says "no sanctions matches" without a corresponding `W5` record is a
**critical defect** (`SCREENING_GAP`) and forces `SCREENING_INCOMPLETE` + gate G3.

The same applies to registry checks ("not found in registry" requires a query record) and media
searches ("no adverse media found" requires the search record with its query strings).

---

## 5. Correction protocol (W9)

```
1. Never edit EV-…; create EV-…-c1 with record_type=CORRECTION,
   supersedes=<original>, correction_reason=<enum>, value=<corrected>.
2. Original gets a back-reference: superseded_by=<new id> (written as a pointer record,
   not a mutation of the original payload).
3. Any finding that cited the original is re-evaluated; if the finding's basis no longer holds,
   its status becomes SUPERSEDED and a new finding is issued.
4. The dossier records the correction in `limitations` / change log, and re-runs SK-12 + SK-13.
5. Corrections after human sign-off require a new run id (RUN-…-R2) — signed reports are immutable.
```

`correction_reason ∈ { OCR_ERROR, WRONG_PAGE, WRONG_QUOTE, MIS_ATTRIBUTION, ARITHMETIC_ERROR,
MISCLASSIFICATION, SUPERSEDED_SOURCE, HUMAN_OVERRIDE }`

---

## 6. Chain integrity & verification

```
verify(ledger):
  prev = GENESIS_HASH
  for record in ledger (ordered by seq):
      assert record.prev_hash == prev
      assert sha256(canonical(record \ {record_hash, signature})) == record.record_hash
      assert verify_sig(record.signature, record.record_hash, run_pubkey)
      prev = record.record_hash
  assert prev == manifest.evidence_ledger.head_hash
```

Run at every stage boundary (cheap: hash chain over appended suffix) and at report emission. A chain
break **blocks delivery** (`INTEGRITY_FAILURE`, gate G3, incident raised). Chain head hash is printed
in the dossier's attestation section so the report and ledger are bound together.

---

## 7. Read access & reviewer experience

| Query | Purpose |
|---|---|
| `by_finding(FND-…)` → evidence[] | "Show me why you said this." |
| `by_section(RPT-…#s4.2)` → evidence[] | Audit one section of the dossier |
| `by_doc(DOC-…)` → extractions[] | "What did you take from this document?" |
| `by_subject(ENT-…)` → evidence[] + queries[] | "What do you know about this person, and what did you check?" |
| `unqueried_party_lists()` | Completeness sweep: any party × list with no `W5`? |
| `chain_verify()` | Integrity proof |
| `replay(run_id)` | Reconstruct the entire run's decision path from ledger + state |

The reviewer UI renders each report claim with the evidence record inline (quote + page image crop +
confidence), so review is a two-click operation rather than a document hunt.

---

## 8. What the ledger deliberately does **not** contain

- Chain-of-thought, deliberation, or scratch reasoning. Only conclusions, evidence, and computed
  traces.
- Full document images (referenced by hash + URI; the bundle is a separate, access-controlled store).
- Special-category personal data, or any protected-characteristic inference.
- Credentials, connector secrets, or internal endpoint details.
- Content from quarantined documents **as decision support** — quarantined records are kept for
  audit (the fact of the injection attempt matters) but their claims are marked `UNVERIFIED` and
  cannot support a finding.
