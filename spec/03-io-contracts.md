# Input / Output Contracts — `ATLAS-CDD` v1.0.0

Field-level contracts. Machine-readable versions in [`../schemas/`](../schemas/).
All IDs are immutable and never reused. All timestamps ISO 8601 UTC. All money as
`{amount: decimal-string, currency: ISO-4217}` — **never** floats.

## ID conventions

| Prefix | Entity | Example |
|---|---|---|
| `CASE-` | Case | `CASE-4831` |
| `RUN-` | Run (one per execution attempt chain) | `RUN-4831-20261020T141233Z` |
| `DOC-` | Document | `DOC-4831-007` |
| `EV-` | Evidence record | `EV-4831-000123` |
| `FND-` | Finding | `FND-4831-0014` |
| `GAP-` / `RFI-` | Gap / RFI request | `GAP-4831-003` / `RFI-4831-R1-01` |
| `HIT-` | Screening hit | `HIT-4831-0002` |
| `ENT-` | Resolved entity (person/org) | `ENT-4831-P03` |
| `ESC-` | Escalation ticket | `ESC-4831-0002` |

---

## 1. INPUT — `CaseInput` (`IN-01`)

```jsonc
{
  "case_id": "CASE-4831",
  "schema_version": "1.0.0",
  "created_at": "2026-10-20T13:02:11Z",
  "case_type": "LEGAL_ENTITY",                 // INDIVIDUAL | LEGAL_ENTITY | TRUST | PARTNERSHIP
  "trigger": "NEW_RELATIONSHIP",               // NEW_RELATIONSHIP | PERIODIC_REVIEW | TRIGGER_EVENT | ALERT_ESCALATION
  "cdd_level_requested": "CDD",                // SDD | CDD | EDD
  "relationship": {
    "products": ["COMMERCIAL_LENDING", "FX"],
    "expected_currency": "USD",
    "expected_monthly_volume": {"amount": "450000", "currency": "USD"},
    "channels": ["RELATIONSHIP_MANAGER"],
    "jurisdictions": ["GB", "AE", "VG"],
    "purpose_statement": "Trade finance for commodity imports"
  },
  "claimed_customer": {                        // claim, never evidence
    "legal_name": "Meridian Trade Holdings Ltd",
    "jurisdiction_of_incorporation": "VG",
    "incorporation_number": "1744882",
    "registered_address": "…",
    "business_nature": "Commodity trading",
    "declared_ubos": [{"name": "Elena Vasquez", "ownership_pct": "60"}]
  },
  "jurisdiction_profile": "US_FED_FINCEN_CDD", // selects policy pack
  "policy_version": "risk-model-2026.3",
  "sla": {"deadline": "2026-10-22T17:00:00Z", "interim_at": "2026-10-21T12:00:00Z"},
  "budget": {"max_usd": 45.00, "max_tokens": 6000000, "max_wallclock_s": 21600},
  "bundle_ref": {"manifest_uri": "s3://kyc-cases/CASE-4831/bundle.json", "doc_count": 47, "page_count": 318},
  "prior_case_refs": ["CASE-1180"],
  "rfi_round": 0,
  "requested_by": {"analyst_id": "U-8842", "desk": "COMMERCIAL_BANKING"},
  "contains_special_category_data": false
}
```

**Validation:** `case_id` unique & required · `case_type` enum · `policy_version` must exist in the
policy registry (else `UNKNOWN_POLICY_VERSION`) · `sla.deadline` in the future · budget ≥ floor ·
`bundle_ref.manifest_uri` resolvable and hash-verified at ingest.

---

## 2. INPUT — `DocumentBundle` (`IN-03`)

Delivered as a manifest + presigned read-only object refs.

```jsonc
{
  "case_id": "CASE-4831",
  "bundle_id": "BND-4831-01",
  "received_at": "2026-10-20T13:00:00Z",
  "channel": "RM_UPLOAD",
  "documents": [
    {
      "doc_id": "DOC-4831-007",
      "filename": "certificate_of_incorporation.pdf",
      "mime": "application/pdf",
      "bytes": 428113,
      "sha256": "9f2c…",
      "declared_type": "CERTIFICATE_OF_INCORPORATION",
      "language_hint": "en",
      "pages": 2,
      "uri": "s3://…/007.pdf",
      "customer_supplied": true,
      "received_from": "customer"
    }
  ]
}
```

Ingest rules: hash-verify on read (mismatch → `INTEGRITY_FAILURE`, document quarantined) · dedupe by
sha256 with all references preserved · reject executables/macros-executed formats · strip active
content from PDFs (JS, embedded files) before rendering · virus scan required before parse.

---

## 3. OUTPUT — `CDDInvestigationReport` (`OUT-01`)

Structure (full JSON Schema in `schemas/cdd-memo.schema.json`):

```jsonc
{
  "report_id": "RPT-4831-01",
  "case_id": "CASE-4831",
  "run_id": "RUN-4831-20261020T141233Z",
  "status": "FINAL_ADVISORY",     // DRAFT | PROVISIONAL | FINAL_ADVISORY | PARTIAL | BLOCKED
  "decision_authority": "HUMAN",  // constant
  "required_gate": "G3",
  "generated_at": "2026-10-20T19:41:02Z",
  "policy_version": "risk-model-2026.3",
  "prompt_version": "atlas-cdd-1.0.0",
  "executive_summary": {
    "text": "…",
    "recommendation": "PROCEED_WITH_CONDITIONS",  // PROCEED | PROCEED_WITH_CONDITIONS | HOLD_FOR_RFI | ESCALATE_EDD | REFER_MLRO
    "confidence": "MEDIUM",
    "key_drivers": [{"factor": "OPAQUE_OWNERSHIP_STRUCTURE", "evidence_refs": ["EV-4831-000214"]}],
    "single_most_important_open_question": "…"
  },
  "sections": {
    "s1_profile": {},
    "s2_identification": {},
    "s3_verification": {},
    "s4_ownership_control": {},
    "s5_purpose_activity": {},
    "s6_screening_media": {},
    "s7_risk_assessment": {},
    "s8_gaps_confidence": {},
    "s9_red_flags": [],
    "s10_recommendation": {},
    "s11_evidence_index": {},
    "s12_attestation": {}
  },
  "findings": ["FND-4831-0001", "FND-4831-0014"],
  "judgments": [{"text": "…", "basis": "judgment", "confidence": "MEDIUM"}],
  "limitations": ["…"],
  "artifact": {"markdown_sha256": "…", "pdf_sha256": "…", "evidence_ledger_head": "…"},
  "human_signoff": null
}
```

**Invariants enforced at write time**

1. `decision_authority == "HUMAN"` — hard-coded, not settable by the agent.
2. If `executive_summary.recommendation ∈ {ESCALATE_EDD, REFER_MLRO}` then `required_gate == "G3"`.
3. Every element of `findings[]` must exist in the ledger and carry ≥1 evidence ref.
4. `status == FINAL_ADVISORY` requires critic `status ∈ {PASS, PASS_WITH_ITEMS}` and coverage ≥ policy minimum.
5. If `coverage.blocks_decision > 0` then `status ∈ {PROVISIONAL, PARTIAL, BLOCKED}` — never FINAL.

---

## 4. OUTPUT — `Finding` (`OUT-03` element)

```jsonc
{
  "finding_id": "FND-4831-0014",
  "case_id": "CASE-4831",
  "type": "UNRESOLVED_UBO",      // see finding type taxonomy below
  "severity": "HIGH",            // INFO | LOW | MEDIUM | HIGH | CRITICAL
  "title": "45% shareholder is a nominee company with undisclosed principal",
  "detail": "Sterling Nominees Ltd holds 45% of the customer's shares; the supplied share register stops at the nominee and no underlying owner is disclosed.",
  "basis": "document",           // document | registry | screening | inference | judgment
  "confidence": "HIGH",
  "evidence_refs": ["EV-4831-000211", "EV-4831-000212"],
  "policy_refs": ["UBO-25", "RDE-07"],
  "rating_impact": {"factor": "OWNERSHIP_OPAQUE", "direction": "UP", "weight_points": 12},
  "status": "OPEN",              // OPEN | MITIGATED | CLOSED | QUARANTINED | SUPERSEDED
  "created_at": "2026-10-20T16:12:44Z",
  "created_by": {"skill": "SK-05", "run_id": "RUN-4831-20261020T141233Z"}
}
```

**Finding types:** `IDENTITY_MISMATCH` · `IDENTIFIER_INCONSISTENT` · `DOCUMENT_EXPIRED` ·
`DOCUMENT_ILLEGIBLE` · `DOCUMENT_AUTHENTICITY` · `CONTRADICTION` · `CORPORATE_STATUS_ALERT` ·
`UNRESOLVED_UBO` · `CIRCULAR_OWNERSHIP` · `NOMINEE_INDICATOR` · `NO_NATURAL_PERSON` ·
`SANCTIONS_HIT` · `PEP_EXPOSURE` · `ADVERSE_MEDIA` · `ENFORCEMENT_ACTION` ·
`UNEXPLAINED_SOURCE_OF_FUNDS` · `ACTIVITY_INCONSISTENT` · `INJECTION_ATTEMPT` ·
`COVERAGE_GAP` · `SCREENING_INCOMPLETE` · `CRITIC_DEFECT`

---

## 5. OUTPUT — `CoverageMatrix` (`OUT-10`)

```jsonc
{
  "case_id": "CASE-4831",
  "policy_version": "risk-model-2026.3",
  "elements": [
    {"rde_id": "RDE-01", "name": "Legal name verified", "required_for": ["CDD","EDD"],
     "status": "SATISFIED", "evidence_refs": ["EV-4831-000031"], "confidence": "HIGH"},
    {"rde_id": "RDE-07", "name": "All beneficial owners ≥25% identified",
     "required_for": ["CDD","EDD"], "status": "PARTIAL", "evidence_refs": ["EV-4831-000211"],
     "reason": "Nominee shareholder undisclosed", "confidence": "LOW", "gap_id": "GAP-4831-003"}
  ],
  "summary": {"total": 24, "satisfied": 19, "partial": 3, "missing": 2,
              "blocks_decision": 2, "coverage_pct": 79.2}
}
```

**RDE baseline (24 elements):** legal name · verified name · entity type/DOB · incorporation/DOB date ·
registered address · verified address · identifier (ID/EIN/reg no.) verified & unexpired ·
nationality/jurisdiction · business nature/occupation · ownership structure chart · all BOs ≥ threshold ·
BO identity verified · control person identified · nature & purpose · expected activity ·
source of funds · source of wealth · sanctions screening (all parties, dated) · PEP screening ·
adverse media (in window) · registry corroboration · document recency · risk rating trace ·
approval/EDD decision record.

---

## 6. OUTPUT — `RunManifest` (`OUT-09`)

```jsonc
{
  "run_id": "RUN-4831-20261020T141233Z",
  "case_id": "CASE-4831",
  "started_at": "2026-10-20T14:12:33Z",
  "finished_at": "2026-10-20T19:41:02Z",
  "status": "COMPLETE",           // COMPLETE | PARTIAL | BLOCKED | AWAITING_HUMAN | FAILED
  "input_hashes": {"case_input": "…", "bundle": "…", "policy": "…"},
  "versions": {
    "agent": "atlas-cdd-1.0.0",
    "prompts": {"system": "sha256:…", "extract": "sha256:…", "critic": "sha256:…"},
    "models": {"extract": "…-pinned", "compose": "…-pinned", "critic": "…-different-family"},
    "tools": {"sanctions.screen": "v4.2.1", "registry.company_search": "v2.0.0"},
    "watchlists": {"OFAC": "2026-10-18", "EU": "2026-10-17", "UK_OFSI": "2026-10-18"}
  },
  "coverage": {"documents_total": 47, "documents_read": 47,
               "pages_total": 318, "pages_read": 314, "pages_illegible": 4,
               "page_ledger_uri": "s3://…/page_ledger.jsonl", "coverage_pct": 98.7},
  "stages": [{"skill": "SK-01", "status": "OK", "duration_ms": 41203, "units": 47},
             {"skill": "SK-15", "status": "OK", "note": "resumed from checkpoint ckpt-000312"}],
  "cost": {"usd": 31.47, "tokens_in": 4180221, "tokens_out": 612880},
  "limitations": ["4 pages of bank statement PDF illegible (scan skew) — see page_ledger"],
  "resumed_from": ["ckpt-000311"],
  "evidence_ledger": {"records": 1841, "head_hash": "…", "uri": "s3://…/evidence.jsonl"}
}
```

---

## 7. Error / status envelope (every skill)

```jsonc
{
  "skill_id": "SK-07",
  "status": "PARTIAL",
  "reason_code": "CONNECTOR_DEGRADED",
  "message": "OFAC connector returned 503 after 5 retries; EU and UK completed.",
  "retryable": true,
  "evidence_ids": ["EV-4831-001190"],
  "gaps": ["GAP-4831-011"],
  "cost": {"usd": 0.42},
  "duration_ms": 88120
}
```

**Reason code vocabulary:** `INVALID_CASE_INPUT` · `INVALID_RUN_CONTEXT` · `BLOCKED_INPUT` ·
`INTEGRITY_FAILURE` · `ILLEGIBLE` · `NOT_FOUND` · `CONNECTOR_DEGRADED` · `SCREENING_INCOMPLETE` ·
`RISK_ENGINE_FAILURE` · `SCHEMA_DECODE_FAILED` · `CITATION_UNRESOLVED` · `CRITIC_FAIL` ·
`BUDGET_EXCEEDED` · `SLA_EXCEEDED` · `AWAITING_HUMAN` · `INJECTION_QUARANTINE` ·
`OUT_OF_SCOPE_DECISION` · `OUT_OF_SCOPE_REPORT` · `OUT_OF_SCOPE_LEGAL` · `OUT_OF_SCOPE_ACCOUNT` ·
`POLICY_VIOLATION` · `INSUFFICIENT_AUTHORITY`
