# Skill Contracts — `ATLAS-CDD` v1.0.0

Each skill is a callable unit with a fixed contract: **inputs → tools → outputs → evidence writes**.
The orchestrator (SK-15) invokes them; the critic (SK-13) audits them.

Common conventions:

- **Evidence write classes:** `W1 register` · `W2 observation` · `W3 extraction` · `W4 computation` ·
  `W5 external-query` · `W6 finding` · `W7 artifact` · `W8 gate/decision-support` · `W9 correction`.
  Full definitions in [`04-evidence-writes.md`](04-evidence-writes.md).
- **Idempotency key:** `sha256(case_id|skill_id|input_hash|prompt_version)`.
- Every skill is **replay-safe**: re-running it with the same key must not duplicate evidence.
- Every skill returns a `SkillResult` envelope:
  `{skill_id, status: OK|PARTIAL|BLOCKED|FAILED, outputs, evidence_ids[], gaps[], confidence, cost, duration_ms, next}`.

---

## SK-01 · Document Triage & Inventory

**Purpose** Decide what each document is, whether it is usable, and what it can prove — before any extraction.

| | |
|---|---|
| **Inputs** | `bundle_manifest` (doc ids, bytes, mime, hashes), `case_type`, `cdd_level` |
| **Tools** | `doc.metadata`, `doc.render_pages`, `doc.classify`, `hash.dedupe`, `lang.detect`, `quality.score` |
| **Process** | Enumerate files → hash → dedupe (keep all refs) → detect type against the 42-type taxonomy in `config/tools.yaml` → page count → language → extractability (native/OCR/illegible) → quality score (blur, skew, crop, contrast, resolution) → recency/expiry check → classify `expected | unexpected | irrelevant | suspicious` |
| **Outputs** | `DocumentManifest` (`document-manifest.schema.json`): per doc `{doc_id, detected_type, declared_type, issuer, doc_date, pages, language, extractability, quality_score, duplicate_of[], recency_verdict, expected_relevance, quarantine_flag}` + `page_ledger[]` |
| **Evidence writes** | `W1` one record per document (hash, type, page count, source location). `W2` one per page only when a fact is drawn from it. |
| **Failure modes** | Encrypted → `BLOCKED_INPUT` + RFI. Over limit → `PARTIAL_INTAKE`. Zero usable docs → G1 gate. |
| **Accept** | 100% of files inventoried; every page accounted for in ledger; duplicates flagged not dropped. |

**Document type taxonomy (42 types, abbreviated):** passport, national ID, driving licence, residence permit, visa, birth certificate, utility bill, bank statement (personal), bank statement (corporate), tax return, tax registration certificate, certificate of incorporation, certificate of good standing, articles/memorandum, share register, shareholder certificate, board resolution, power of attorney, trust deed, partnership agreement, UBO declaration, org chart, corporate registry extract, LEI record, audited financial statements, management accounts, invoices, contracts, licence/permit, website printout, corporate brochure, sanctions/PEP screening report, adverse media printout, source-of-funds letter, source-of-wealth statement, sale/purchase agreement, loan agreement, payslip, employment letter, reference letter, court filing, other/unclassified.

---

## SK-02 · Layout-Aware Ingestion & OCR

**Purpose** Turn pages into structured, anchored, quotable text — preserving tables, stamps and page geometry.

| | |
|---|---|
| **Inputs** | `doc_id`, page range |
| **Tools** | `doc.read_page`, `doc.ocr`, `table.extract`, `vlm.describe_region`, `stamp.detect` |
| **Process** | Native text layer first when confidence high → else OCR (layout-aware) → table detection (never split a row; emit as structured cells) → stamp/seal/handwriting detection → emit page text with character spans and bbox per block |
| **Outputs** | `PageContent {doc_id, page, blocks[{text, char_span, bbox}], tables[{cells, headers}], stamps[], ocr_confidence, method}` |
| **Evidence writes** | `W2` on extraction of any quoted span — the quote, the span, the method, the OCR confidence. |
| **Failure modes** | OCR conf < 0.6 → mark page ILLEGIBLE, RFI, no guessing. Mixed-script pages → per-block language tag. |
| **Accept** | Every quote in the dossier must resolve to a `PageContent.block.char_span`; span-existence is machine-checked by SK-13. |

> **Rule:** the *source quote in evidence is always the original-language string*. Translations are
> stored as `derived.normalized_value` with `translation_of` pointing at the source evidence id.

---

## SK-03 · Typed Field Extraction

**Purpose** Populate the CDD data schema with facts, each one anchored to a page.

| | |
|---|---|
| **Inputs** | `PageContent[]`, `extraction_schema` (per doc type), `case_type`, `rde_list` |
| **Tools** | `llm.extract_constrained`, `validate.schema`, `normalize.datetime`, `normalize.address`, `normalize.identifier` |
| **Process** | Per-document extraction with constrained decoding → per-field: `value`, `normalized_value`, `page`, `char_span`, `verbatim_quote`, `confidence`, `basis` → normalizers (ISO dates, ISO 3166 countries, DOB formats, ID checksum flags, address parsing) → attach `doc_authority` weight |
| **Outputs** | `ExtractedFact[]` grouped by entity and field path; per-field provenance |
| **Evidence writes** | `W3` per extracted field with a material value (names, IDs, dates, amounts, %, addresses, roles). Non-material boilerplate is not evidence. |
| **Failure modes** | Schema-blocked decode → retry with narrowed schema → else `UNRESOLVED` + reason. Handwriting → `confidence ≤ MEDIUM` always. |
| **Accept** | Field-level F1 ≥ 0.95 (IDs ≥ 0.99) on golden set; 100% of extracted facts carry a resolvable span. |

**Extraction field set (individual):** full_name, name_variants[], dob, gender (only if on an official ID and relevant to matching), nationality[], residency[], address_current, address_history[], id_type, id_number, id_issue_date, id_expiry, id_issuer, tax_id, occupation, employer, source_of_funds, source_of_wealth, income_band, contact, marital_status (only if relevant to matching), pep_self_declaration, sanctions_self_declaration.

**Extraction field set (legal entity):** legal_name, trading_names[], entity_type, incorporation_number, incorporation_date, jurisdiction_of_incorporation, registered_address, operating_address, business_nature, sic/naics, lei, tax_id, share_capital, share_classes[], officers[], directors[], signatories[], shareholders[], auditor, financial_year_end, revenue, employees, parent, subsidiaries, listing_status, regulated_status.

---

## SK-04 · Entity Resolution & Linking

**Purpose** Decide, defensibly, which mentions across documents refer to the same person or organisation.

| | |
|---|---|
| **Inputs** | `ExtractedFact[]`, existing `entity_index` |
| **Tools** | `entity.canonicalize`, `name.transliterate`, `similarity.match`, `id.match`, `graph.link` |
| **Process** | Canonicalize (case, diacritics, name order, suffixes, honorifics, maiden/former names, transliteration variants) → match on identifiers first (exact) → then on attribute bundles (name + DOB + nationality, or name + address + registration no.) → assign `MERGE_CONFIDENCE` → require **≥2 corroborating attributes** for an automatic merge; otherwise `PROBABLE_MATCH` and keep both live |
| **Outputs** | `EntityIndex {entity_id, entity_type, canonical_name, aliases[], attributes, source_docs[], merge_confidence, open_questions[]}`, `Contradiction[]` |
| **Evidence writes** | `W3` for each attribute used in the merge; `W6` for each `PROBABLE_MATCH` and each `CONTRADICTION`. |
| **Failure modes** | Common-name collisions → never auto-merge on name alone. Transliteration ambiguity (Mohammed/Muhammad/Mohamed) → keep variants as aliases of one entity *only* with a second attribute. |
| **Accept** | Zero name-only merges in audit sample; every contradiction surfaced, none silently resolved. |

---

## SK-05 · Ownership Graph & UBO Computation

**Purpose** Answer the only question that matters here: **which natural persons ultimately own or control this customer, and by how much?**

| | |
|---|---|
| **Inputs** | `EntityIndex`, `ExtractedFact[]` (shareholders, percentages, share classes, voting rights), registry data, `policy.ubo_threshold` |
| **Tools** | `graph.build`, `calc.ownership`, `graph.cycles`, `graph.paths`, `policy.threshold` |
| **Process** | Build nodes (natural persons, legal entities, trusts, states, unknown) and directed edges `owner → owned` with `{percentage, share_class, voting_pct, as_of, evidence_id}`. Detect cycles and nominee/trust constructs. Compute effective ownership: for each natural person, enumerate every path to the customer, multiply percentages along each path, sum across paths, cap at 100%. Identify `≥ threshold` holders. If none, resolve the **control prong**: senior managing official, board control, voting majority, trust settlor/trustee/beneficiary, partnership GP. Enumerate **unresolved links** — a node with no natural person at the end is a finding, not a footnote. |
| **Outputs** | `OwnershipGraph {nodes[], edges[], effective_ownership[], control_prong{person, basis, evidence}, unresolved_links[], structures{circular, nominee, bearer, differential_voting, trust}}`, plus a per-path arithmetic trace |
| **Evidence writes** | `W3` per shareholding/directorship fact; `W4` for every computed effective-%; `W6` for `UNRESOLVED_UBO`, `CIRCULAR_OWNERSHIP`, `NOMINEE_INDICATOR`, `NO_NATURAL_PERSON`. |
| **Failure modes** | Percentages absent → `UNRESOLVED`, do not assume equal split. Sum > 100% (data error or multi-class) → flag, show class split, do not normalise silently. Chain ends at another company → recurse to `max_depth` (policy default 4); beyond that → `UNRESOLVED_UBO` + RFI. |
| **Accept** | Arithmetic reproducible by hand from the trace; every BO has ≥1 evidence-backed edge chain; control-prong determinations state their basis. |

**Arithmetic contract (example):**
```
Customer: Meridian Trade Holdings Ltd (BVI 1744882)
Path A: Elena Vasquez → 60% → Northbridge Holdings Ltd → 45% → Meridian  = 27.00%
Path B: Elena Vasquez → 100% → Elm Family Trust → 10% → Meridian        = 10.00%
Path C: Viktor Sokolov → 40% → Northbridge Holdings Ltd → 45% → Meridian = 18.00%
Effective: Vasquez 37.00% (BO), Sokolov 18.00% (below 25% → control prong test)
Unresolved: 45% held by "Sterling Nominees Ltd" — no underlying owner disclosed  [EV-...]
```

---

## SK-06 · Registry & Document Corroboration

**Purpose** Check the customer's story against authoritative public filings, and check documents against each other.

| | |
|---|---|
| **Inputs** | `EntityIndex`, jurisdictions, `registry` connector, `IN-07` |
| **Tools** | `registry.company_search`, `registry.filing_get`, `registry.officers`, `registry.charges`, `doc.cross_check` |
| **Process** | For each legal entity: search registry by name + number → pull status (active/dissolved/struck off/liquidation), incorporation date, registered address, officers, share capital, last filing date → compare with supplied documents → record matches/mismatches. Check document recency (statements ≤ policy window, IDs unexpired, registry extract ≤ policy window). Corporate existence check: is the entity still alive at run date? |
| **Outputs** | `CorroborationResult {entity, checks[{attribute, supplied, registry, verdict: MATCH|MISMATCH|NOT_FOUND|UNAVAILABLE, evidence_ids[]}]}` |
| **Evidence writes** | `W5` per registry query with dataset + query date + raw reference; `W6` per mismatch. |
| **Failure modes** | No registry coverage for jurisdiction → record `NOT_AVAILABLE` and raise as a jurisdiction limitation; never infer. Dissolved entity → `CORPORATE_STATUS_ALERT`, gate G3. |
| **Accept** | Every legal entity has ≥1 registry query attempt logged; mismatches all visible. |

---

## SK-07 · Screening & Hit Triage

**Purpose** Query every required party against every required list, then triage hits defensibly — and never self-clear a true hit.

| | |
|---|---|
| **Inputs** | `Subject[]` = customer + BOs + directors/officers + signatories + salient related parties; `screening_config` |
| **Tools** | `sanctions.screen`, `pep.screen`, `enforcement.screen`, `media.screen`, `watchlist.versions` |
| **Process** | Build the subject list with full attribute bundle (names + aliases + DOB + nationality + ID numbers + countries) → run each connector → capture dataset version + query timestamp → score hits (`T-01`) → triage each hit on discriminating attributes: name exactness, DOB, nationality, country, identifiers, occupation, entity type → assign `TRUE / STRONG / WEAK / FALSE` with named discriminators → for TRUE/STRONG: **stop, recommend escalation, do not clear** |
| **Outputs** | `ScreeningResults {subjects[], queries[{subject_id, list, dataset_version, queried_at, hits[]}], hits[{hit_id, match_score, matched_attributes, discriminators, strength, recommended_disposition, analyst_required:true}], coverage{lists_run[], lists_unavailable[]}}` |
| **Evidence writes** | `W5` per query (mandatory, including zero-hit queries — absence of evidence of a hit requires evidence of the query); `W6` per hit triage. |
| **Failure modes** | Connector down → retry/backoff → `SCREENING_INCOMPLETE`, force G3, never mark "no hits". List stale beyond policy freshness → flag and re-run on fresh list if available. |
| **Accept** | 100% subject × list coverage recorded; zero TRUE hits closed by the agent; query dates present on every result. |

**Discriminator weights (default, policy-overridable):** exact ID number match = dispositive-negative for exclusion; DOB match +0.30; nationality match +0.15; name exact +0.20, alias match +0.15, fuzzy ≥0.85 +0.10; country of residence/operation match +0.15; occupation/sector match +0.10; entity-type match +0.05. Thresholds: ≥0.85 TRUE, 0.65–0.85 STRONG, 0.40–0.65 WEAK, <0.40 FALSE. **Any dispositive attribute conflict (e.g. DOB differs by >2 years, different passport number) caps at WEAK.**

---

## SK-08 · Adverse Media & Open-Source Research

**Purpose** Find what a diligent analyst would find in 45 minutes of open-source research — and document it so it can be defended.

| | |
|---|---|
| **Inputs** | `Subject[]`, `media_policy {lookback_years, categories, source_tiers, min_tier}` |
| **Tools** | `media.search`, `web.fetch_allowlisted`, `source.tier`, `event.dedupe`, `timeline.build` |
| **Process** | Build queries per subject (name + alias × category terms × jurisdiction) → search allowlisted tiers only (Tier 1: court/regulator/official gazette; Tier 2: established financial press; Tier 3: reputable national press; Tier 4: trade/local — corroboration only) → dedupe syndicated copies → extract `{event_date, category, allegation_stage, source, tier, url, quote, entities_named, corroboration_count}` → build an event timeline → relevance-test against policy categories and lookback window |
| **Outputs** | `AdverseMediaResult {subjects[], events[], exclusions[{item, reason}], source_coverage{queries_run, sources_tiered}, residual_concern: NONE|LOW|MEDIUM|HIGH}` |
| **Evidence writes** | `W5` per query with engine + date; `W3` per retained event with the quoted passage and source URL; `W6` for each relevance decision. |
| **Failure modes** | Paywalled/unfetchable source → record `UNABLE_TO_VERIFY` with the citation; do not rely on the headline alone. Same-name different-person → apply the same discriminator discipline as SK-07 and record the exclusion and why. |
| **Accept** | Every retained event has a Tier rank + verification status; every exclusion logged with a reason; allegations never stated as findings of fact. |

**Prohibited:** protected-characteristic research; rumour aggregation sites as sole source; scraping behind logins; contacting subjects; recording political opinions beyond PEP status.

---

## SK-09 · Activity Profile & Source of Funds/Wealth

**Purpose** Test whether the relationship makes sense, and say so in language that is defensible.

| | |
|---|---|
| **Inputs** | `ExtractedFact[]` (business nature, revenue, occupation, income), `IN-06` transactional summary if available, `product_set`, `geography` |
| **Tools** | `activity.derive_expected`, `txn.summarize`, `peer.benchmark`, `calc.ratio` |
| **Process** | Derive expected profile from sector/revenue/geography/product (expected monthly volume, counterparty geographies, instruments) → compare with observed if available → test SoF against declared income/revenue, test SoW against declared background and asset base → flag `CONSISTENCY: CONSISTENT | PLAUSIBLE_UNVERIFIED | INCONSISTENT | UNEXPLAINED` with reasons → derive monitoring thresholds for handoff |
| **Outputs** | `ActivityProfile {expected{}, observed{}, consistency_flags[], sof_assessment, sow_assessment, monitoring_handoff{volume_bands, counterparty_countries, instruments}}` |
| **Evidence writes** | `W3` per input fact; `W4` per computed ratio; `W6` per inconsistency finding. |
| **Failure modes** | No observed data → output expected profile only, mark `OBSERVED: NOT_AVAILABLE`; do not imply absence of activity. |
| **Accept** | Every consistency flag names the comparison pair and both inputs; no criminality conclusions. |

---

## SK-10 · Risk Scoring & CDD-Level Determination

**Purpose** Produce a rating that a reviewer and an auditor can both recompute.

| | |
|---|---|
| **Inputs** | `RiskFactorInputs[]` from SK-03..SK-09, `policy.risk_model` (versioned), `jurisdiction_tiers` |
| **Tools** | `policy.risk_engine` (deterministic function, not an LLM) |
| **Process** | Assemble enumerated factor inputs with evidence refs → call engine → receive `inherent_score`, `mitigant_score`, `residual_score`, `band`, `cdd_level`, `review_cadence`, `trace[]` → write narrative that *explains* the trace without altering it |
| **Outputs** | `RiskRating {model_version, inherent{score,band,factors[]}, mitigants[], residual{score,band}, cdd_level, review_cadence, trace[], unresolved_inputs[], override_note}` |
| **Evidence writes** | `W4` for every factor input and the final score; `W6` if any factor is UNRESOLVED. |
| **Failure modes** | Missing factor input → use worst-case endpoint, label `provisional_pending_gap`. Engine error → `RISK_ENGINE_FAILURE`, block delivery of a final rating, force G3. **Never hand-compute a rating.** |
| **Accept** | Independent recomputation from the trace yields an identical score; zero factors outside the permitted vocabulary. |

**Default model shape** (full weights in `config/policy.yaml`): inherent = weighted sum over {customer 35%, product 20%, geography 25%, channel 10%, behaviour 10%} → banded 0–39 LOW / 40–59 MEDIUM / 60–79 HIGH / 80–100 VERY_HIGH; mitigants subtract capped points (documented, verified, in-date evidence); residual band drives CDD level: LOW→SDD eligible, MEDIUM→CDD, HIGH/VERY_HIGH→EDD mandatory.

---

## SK-11 · Gap Analysis & RFI Generation

**Purpose** Convert every hole in the evidence into an actionable, prioritized request.

| | |
|---|---|
| **Inputs** | `rde_list`, findings, coverage from all prior skills |
| **Tools** | `coverage.compute`, `rfi.compose`, `policy.rde_lookup` |
| **Process** | For each required data element assign `SATISFIED | PARTIAL | MISSING | CONTRADICTED | ILLEGIBLE | NOT_APPLICABLE` with evidence ref or reason → impact-score each gap (which rating factor it moves, by how much, and whether it blocks the decision) → compose customer-ready request text (specific, respectful, minimal, with acceptable alternatives) → priority P1/P2/P3 |
| **Outputs** | `GapPack {coverage_matrix[], gaps[{gap_id, rde, status, reason, blocks_decision, rating_impact, priority}]}`, `RFIPack {request_id, items[{gap_ids, ask, acceptable_forms[], why}], round, deadline}` |
| **Evidence writes** | `W6` per gap; `W7` for the RFI artifact. |
| **Failure modes** | Gap is customer-unfixable (e.g. jurisdiction has no registry) → classify `STRUCTURAL`, propose an alternative control (e.g. certification by local counsel), do not loop the RFI. |
| **Accept** | Every non-SATISFIED element appears in exactly one RFI item; every P1 gap states which decision it blocks. |

---

## SK-12 · Dossier Composition

**Purpose** Write the decision-ready report.

| | |
|---|---|
| **Inputs** | All prior outputs + evidence index |
| **Tools** | `llm.compose`, `render.markdown`, `render.pdf`, `citation.bind` |
| **Process** | Retrieve only the cited spans (not the corpus) → write the 12 mandated sections → bind every material sentence to evidence ids → never restate a number the engine produced without its ref → include methodology + limitations appendix |
| **Outputs** | `CDDInvestigationReport` (`cdd-memo.schema.json`) + rendered Markdown/PDF |
| **Evidence writes** | `W7` for the artifact; `W6` for each finding referenced; report `artifact_hash` recorded. |
| **Failure modes** | Claim cannot be cited → either delete it or move it to `judgments[]` labelled as judgment. Never leave it uncited. |
| **Accept** | Zero uncited material sentences after SK-13; all 12 sections present; executive summary ≤ 400 words. |

---

## SK-13 · Adversarial Critic / QA

**Purpose** Try to break the dossier before a regulator does. Runs on a **different model family** from SK-12.

| | |
|---|---|
| **Inputs** | Draft dossier, full evidence ledger, policy config |
| **Tools** | `citation.verify` (span-exists + quote-matches), `math.recompute`, `policy.conformance`, `contradiction.scan`, `injection.scan`, `rde.coverage` |
| **Process** | (a) **Citation audit** — every cited evidence id exists, the quote is verbatim and present at the stated page/span, and the quote *actually supports* the claim; (b) **recompute** every number from its trace; (c) **contradiction scan** — facts inconsistent within the pack; (d) **policy conformance** — required steps, required fields, screening coverage, permissioned factors; (e) **basis audit** — no `inference` in a `verified_*` field, no `judgment` presented as fact; (f) **injection & leakage scan** — quarantined content influencing conclusions, protected-characteristic language, chain-of-thought leakage; (g) **over-claiming scan** — certainty language exceeding evidence strength |
| **Outputs** | `CritiqueResult {status: PASS|PASS_WITH_ITEMS|FAIL, defects[{class, severity, location, claim, expected, evidence_id, fix}], stats{citations_checked, validated, failed, numbers_recomputed, mismatches}}` |
| **Evidence writes** | `W8` for the critique artifact; `W6` for each unrepaired defect (these become visible limitations). |
| **Failure modes** | ≥1 unrepaired CRITICAL after 2 repair loops → case confidence downgraded, forced G3, defect surfaced in dossier limitations. |
| **Accept** | Citation precision ≥ 99.5%; zero unreported critical defects. |

**Defect classes:** `UNCITED_CLAIM`, `QUOTE_MISMATCH`, `WRONG_PAGE`, `UNSUPPORTED_INFERENCE`, `ARITHMETIC_ERROR`, `CONTRADICTION`, `POLICY_SKIP`, `SCREENING_GAP`, `PROTECTED_CHARACTERISTIC`, `INJECTION_INFLUENCE`, `OVERCLAIM`, `MISSING_LIMITATION`.

---

## SK-14 · Escalation & Human Gates

**Purpose** Hand off cleanly, and pause/resume without losing work.

| | |
|---|---|
| **Inputs** | Case state, findings, critic result, `policy.escalation_matrix` |
| **Tools** | `human.task_create`, `notify.send`, `state.pause`, `case.route` |
| **Process** | Evaluate gate conditions (G1–G4) + escalation triggers → create the human task with a decision-ready summary (not the whole dossier), the specific question, the options, and the evidence → pause the run with `AWAITING_HUMAN` → on resume, ingest the decision as an evidence record (`W8`) and continue → if a human *overrides* the engine, record the override verbatim, the person, the timestamp, and the reason code; never modify the engine trace |
| **Outputs** | `EscalationTicket {ticket_id, gate, severity, reason_codes[], summary, question, options[], routed_to, sla}`, `HumanDecisionRecord` |
| **Evidence writes** | `W8` per gate event, including who, when, what, on what evidence. |
| **Failure modes** | No human available before SLA → escalate to secondary queue, deliver `PROVISIONAL` dossier. |
| **Accept** | Every gate event is in the ledger with an owner and timestamp; ≥HIGH cases never reach `FINAL` without G3. |

**Reason codes:** `SANCTIONS_HIT`, `PEP_EXPOSURE`, `ADVERSE_MEDIA_MATERIAL`, `UBO_UNRESOLVED`, `OWNERSHIP_OPAQUE`, `CONTRADICTION_MATERIAL`, `DOCUMENT_AUTHENTICITY`, `INJECTION_ATTEMPT`, `COVERAGE_BELOW_MINIMUM`, `RISK_VERY_HIGH`, `SCREENING_INCOMPLETE`, `BUDGET_EXCEEDED`, `CRITIC_FAIL`.

---

## SK-15 · Case State & Long-Run Manager

**Purpose** Make a 6-hour, 400-page investigation behave like a reliable service: resumable, observable, cheap enough, and honest about progress.

| | |
|---|---|
| **Inputs** | Case state store, queue, budget meter |
| **Tools** | `state.checkpoint`, `state.resume`, `queue.enqueue`, `budget.meter`, `progress.emit`, `dlq.push` |
| **Process** | Drive the stage machine (§5 of the system prompt) → fan out per-document work with bounded concurrency → checkpoint after each unit (`run-state.schema.json`) → idempotency-key every unit → retry with exponential backoff + jitter → dead-letter after N attempts → heartbeat + EWMA ETA → stall detector → budget meter with hard stop → interim delivery at SLA-30% → emit progress events |
| **Outputs** | `RunState`, `RunManifest`, `ProgressEvent[]` |
| **Evidence writes** | `W1` (run start: input hashes, versions) and `W8` (stage transitions). Never fabricate a `page_ledger` entry for a page not processed. |
| **Failure modes** | Crash → resume from last checkpoint, skip completed units by idempotency key. Partial OCR outage → degrade to native-text-only, mark affected pages. Budget hit → `PARTIAL_DELIVERY`. |
| **Accept** | Kill-and-resume at 20 random points per test: 100% completion, zero duplicate evidence, page ledger identical to uninterrupted run. |

---

## Skill dependency graph

```
SK-01 → SK-02 → SK-03 → SK-04 → SK-05 ─┬→ SK-09 → SK-10 → SK-11 → SK-12 → SK-13 → SK-14
                    │            │       │
                    └→ SK-06 ────┘       │
                    └→ SK-07 ────────────┘
                    └→ SK-08 ────────────┘
SK-15 wraps everything (state, budget, concurrency, checkpoint/resume)
SK-11 may re-enter SK-01/03 after RFI responses (round 2+, bounded by policy max_rfi_rounds=2)
```
