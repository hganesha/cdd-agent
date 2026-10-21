# 02 · Skills

Twelve skills. Each is specified with **inputs**, **tools**, **outputs**, **evidence writes**,
**failure behaviour** and **acceptance tests**. The acceptance column names the test class in
`tests/test_search_agent.py` that fails if the skill regresses; every one of them is executable.

Legend: **[code]** = deterministic Python, no model. **[model]** = a model call, temperature 0,
constrained to the templates in the prompt, output schema-validated. Nothing that decides
trustworthiness is a model call.

---

## SK-S1 · Query planning and facet coverage

| | |
|---|---|
| Kind | [code] `plan_queries()` + [model] `SYS-PLAN` for selection/ordering |
| Purpose | Turn a subject into a bounded, facet-complete query set, including each related party under their own name |

**Inputs**
* `subject` (required, from the request)
* `facets[]` — default `IDENTITY`, `ADVERSE`, `REGULATORY`, `CORPORATE_STATUS`
* `related_parties[]` — UBOs / directors; each generates its own `RELATED_PARTY` queries
* `max_queries` — hard cap, further capped by `config/budget.yaml: max_queries`

**Tools** — `FACETS` template table (code); model may order and select within it. Unknown facet →
`KeyError` (loud, not silent).

**Outputs** — `PlannedQuery(query_id=Q-nn, text, facet, rationale)`, ordered, unique ids.

**Evidence writes** — one `QUERY` record per planned query:
`{record_type: QUERY, request_id, query: {query_id, text, facet, rationale}}`, idempotent on
`(request_id, query_id)` — a resumed run never duplicates a query record.

**Failure** — no name, or a name too generic to disambiguate → `status: FAILED`,
`gaps: [{gap: NO_RESULTS, detail: "…"}]` naming the attribute that would have helped. Never "search
harder with a vaguer query".

**Acceptance** — `TestSKS1QueryPlanning` (4 tests): ids sorted and unique; every query carries a
rationale; `max_queries` is a hard cap; a related party's queries contain the person's name and not
the entity's; unknown facet raises.

---

## SK-S2 · Source policy: tier, deny-list, citability

| | |
|---|---|
| Kind | [code] `SourcePolicy` |
| Purpose | Decide, before any content is read, whether a domain may be fetched and whether what comes back may be cited |

**Inputs** — `config/sources.yaml` (schema-validated, versioned): tiers 1–5 with weights and
`citable_alone`, `denied_domains`, `max_tier_citable`, `tier4_corroboration_only`, compliance block.

**Tools** — `tier_of(domain)`, `may_fetch(domain)`, `citable(fetch)`.

**Outputs** — `(allowed, reason)` and `(citable, reason)`; the resolved `source_tier` on every
`FetchResult`.

**Evidence writes** — `RETRIEVAL` record with `status` and `reason` for every URL considered,
citable or not.

**Rules that the tests pin**
* A denied domain is refused **before its body is read** — `FetchResult.body == ""`,
  `content_hash == ""`. Nothing from a denied domain can reach a quotation.
* `max_tier_citable: 3` → a tier-4 source is retrievable (its circulation is a fact) but never
  citable.
* **A page cannot self-declare a higher tier.** If `sources.yaml` classifies the domain, that
  classification wins; the page's own `source_tier` field is used only when the policy has no
  opinion (tier 5 = unclassified).

**Failure** — unknown domain → tier 5, not citable, and the refusal reason says so.

**Acceptance** — `TestSKS2SourcePolicy` (4 tests), including
`test_tier_cannot_be_self_declared_upward_by_a_page`.

---

## SK-S3 · Retrieval: robots, paywalls, licensing, rate

| | |
|---|---|
| Kind | [code] `Fetcher.fetch()` |
| Purpose | Retrieve a page within the publisher's rules, or establish precisely why not |

**Inputs** — `url`, `retrieved_at` (pinned clock), `config/sources.yaml: compliance`.

**Tools** — robots check (strictest interpretation), paywall detection (metadata retained, body not
citable), licence check, per-domain delay (`min_delay_ms_between_fetches_per_domain: 1000`),
declared user agent, URL-hash cache (`out/cache/`).

**Outputs** — `FetchResult(url, status, title, published, author, body, source_tier, kind,
licensing, lang, content_hash, retrieved_at, injected, injection_evidence)`. Statuses: `OK`,
`BLOCKED_DOMAIN`, `ROBOTS_DISALLOWED`, `PAYWALLED`, `NOT_FOUND`.

**Evidence writes** — `RETRIEVAL` with `url`, `status`, `content_hash`, `retrieved_at`. The hash is
what makes every later quotation verifiable.

**Failure** — paywalled → `gaps: [{gap: PAYWALLED_SOURCE, detail: "<url> — metadata only, body not
citable"}]` and the page never appears in a citation. Robots-disallowed → body discarded, listed in
`refused_sources`. Budget exhausted → `FETCH_BUDGET_EXHAUSTED`, `status: PARTIAL`, checkpoint
written.

**Acceptance** — `TestSKS3Compliance` (3 tests): robots body discarded; paywall → gap + never cited;
fetch budget stops the run at the limit (per *fetch*, not per query) and declares the gap.

---

## SK-S4 · Passage extraction with exact character spans

| | |
|---|---|
| Kind | [code] `extract_passages()` |
| Purpose | Cut the citable sentences out of a body such that each quotation can be re-derived from the source |

**Inputs** — a citable `FetchResult`, query keywords (for ranking), `max_passages` (default 4).

**Tools** — sentence segmentation; keyword scoring; whitespace-aware span arithmetic.

**Outputs** — `Passage(passage_id, url, title, quote, char_span, content_hash, published,
source_tier, kind, licensing, retrieved_at)`.

**Invariant (the one that matters)** — `body[char_span[0]:char_span[1]] == quote`, byte for byte.
Leading whitespace moves the offsets, not just the text. `webis.cli verify` re-derives every span
from the corpus and re-hashes the body; a drift of one character is a failed run.

**Evidence writes** — passages are not written individually; they travel inside `CLAIM` records
(with span and hash) so that a claim and its proof are one record.

**Failure** — a sentence shorter than 40 characters is not a passage (it is a headline fragment and
cannot carry a fact alone). A non-citable fetch yields no passages at all.

**Acceptance** — `TestSKS4PassageExtraction` (3 tests), including a whole-corpus span re-slice; plus
`webis.cli verify` at 100% quote precision in the definition of done.

---

## SK-S5 · Credibility scoring, factor by factor

| | |
|---|---|
| Kind | [code] `credibility()` |
| Purpose | Produce a 0–100 score whose every point can be explained to a reviewer |

**Inputs** — a `Passage`, the set of corroborating domains, the set of contradicting domains, the
source policy weights.

**Tools** — additive scoring, factors recorded:
`source_tier` (45/30/18/5/0), `independent_corroboration` (10 per other domain, capped at 20),
`cites_primary_record` (+10 when the text references a registry filing, court record, final notice
or public register), `primary_document` (+15 for a regulator notice/report),
`contradiction` (negative), `recency` (declared, not implicit), `paywall` (metadata-only penalty).

**Outputs** — `{score, band, factors: [{factor, value, points}]}` with
`sum(points) == score` (tested to 6 decimal places).

**Evidence writes** — embedded in each citation in `CLAIM` and `PACK` records.

**Deliberate design choice** — the score is *not* a truth probability and is never presented as one.
It answers a narrower question: how much weight may this quotation carry, given who published it and
who else says the same thing. Confidence (`SK-S7`) is derived from origins and score together.

**Acceptance** — `TestSKS5Credibility` (4 tests): tier ordering; corroboration adds and is capped at
20; contradiction subtracts; score bounded 0–100 and exactly the sum of its factors.

---

## SK-S5b · Same-name discrimination

| | |
|---|---|
| Kind | [code] `SearchAgent.discriminates()` |
| Purpose | Refuse a passage that is about a different person or entity with the same name |

**Inputs** — `attributes: {dob, country, registration_number}`, the passage text.

**Tools** — conflict detection: date-of-birth year (±1 tolerance), inferred age (±2), location
(a country-specific city list, e.g. `Valencia` against a Venezuela-domiciled subject), registration
number mismatch.

**Outputs** — `(keep: bool, reason: str)`. A conflicting attribute → excluded, with the conflict
stated in the reason and recorded as `refused_sources: {status: EXCLUDED_SAME_NAME, reason, quote}`.

**Rule** — **absence of an attribute is not a conflict.** A passage that mentions no birth date is
kept, not guessed away. Exclusion requires positive evidence of difference; the error to avoid is
deleting the true adverse media because a namesake exists.

**Evidence writes** — `EXCLUDED_SAME_NAME` entries in the pack, never a silent drop. (A reviewer who
sees three exclusions knows the name is common; a reviewer who sees nothing knows nothing.)

**Acceptance** — `TestSKS5bSameName` (5 tests): DOB, age and location conflicts each excluded with
the right reason; an attribute-free passage kept; the exclusion actually lands in the pack.

---

## SK-S6 · Triangulation and independence by origin

| | |
|---|---|
| Kind | [code] `triangulate()`, `attribution_origin()`, `unresolved_attribution()` |
| Purpose | Decide how many *independent* sources assert a fact — the difference between corroboration and repetition |

**Inputs** — findings (claim_key + passage), the fetched documents keyed by URL.

**Tools** — attribution parsing (`according to`, `as first reported by`, `citing`, `based on`, `a
report by/from`, up to four capitalised words), matched against candidate domains by squeezed stem
(`Global Wire` ≡ `GlobalWire` ≡ `globalwire.example`).

**Outputs** — per claim: `origins: {origin: [domains]}`, `corroboration_count = len(origins)`,
`syndication_detected = len(independent_domains) > corroboration_count`, and `notes` for
(a) an attribution to an origin we never retrieved (an *undercount* warning) and (b) a single origin
reached through several outlets.

**The two hard cases**
1. **Origin fetched.** `commoditytoday` reprints Global Wire → both resolve to `globalwire.example`
   → one origin, note added.
2. **Origin not fetched.** The outlet names "Global Wire" and we never retrieved it. The origin key
   is the *attributed name*, so it still collapses to one origin, and the note says the corroboration
   count may be an undercount rather than implying independence.

**Evidence writes** — `origins`, `corroboration_count`, `syndication_detected` and `notes` on the
`CLAIM` record.

**Acceptance** — `TestSKS6Triangulation` (3 tests): syndicated pair collapses to 1 with a note;
genuinely independent pair stays 2 with `syndication_detected: false`; unretrieved origin is
disclosed rather than silently counted.

---

## SK-S7 · Citation pack and confidence

| | |
|---|---|
| Kind | [code] `SearchAgent.pack()` + [model] `SYS-CLAIM` for the sentence only |
| Purpose | Assemble the only artefact a downstream agent may consume |

**Inputs** — triangulated groups, passages, policy version, request id.

**Tools** — citation assembly; one citation entry **per URL** with `evidence_ids[]` for the extra
passages (three passages from one document are one source, not three); confidence derivation:

| Confidence | Condition |
|---|---|
| `HIGH` | best citation ≥ 70 and ≥ 2 independent origins |
| `MEDIUM` | best citation ≥ 55, or ≥ 2 origins with a lower score |
| `LOW` | single origin, any score |
| `UNSUPPORTED` | no citation survives (claim not citable) |

**Outputs** — `out/evidence_pack.json` (`schemas/evidence-pack.schema.json`): `pack_id`,
`request_id`, `subject`, `status`, `claims[]`, `quarantined_sources[]`, `refused_sources[]`,
`unverified_circulation[]`, `gaps[]`, `cost{queries, fetches, domains}`, `source_policy_version`,
`method`, `query_log`.

**Evidence writes** — a `CLAIM` record per claim (including citations with spans and hashes) and one
`PACK` record carrying `pack_hash` over the whole pack.

**Failure** — no claim is emitted without a citation: the schema rejects it, and `pack()` drops any
group whose citations do not resolve. A pack with zero claims is legal and means "nothing citable
found", which is different from "nothing found" and is reported as such.

**Acceptance** — `TestSKS7CitationPack` (5 tests) plus `TestContracts`: every claim cited; one
citation per URL; a single tier-3 source cannot reach `HIGH`; determinism (identical claims for
identical input); tier-4 circulation recorded but never cited.

---

## SK-S8 · Evidence ledger

| | |
|---|---|
| Kind | [code] `EvidenceLedger` |
| Purpose | Make the run reconstructible: what was asked, what was fetched, what was claimed, in order, tamper-evident |

**Inputs** — every write class above.

**Tools** — append-only JSONL; `evidence_id = WS-nnnnnn`; canonical JSON (sorted keys, no
whitespace) hashed with SHA-256; `prev_hash` chaining; `signature = kms-stub:<hash prefix>`;
`IdempotencyStore` keyed on `idem_key(...)` so retries and resumes are no-ops.

**Outputs** — `out/search_evidence.jsonl`; each line validates against
`schemas/evidence-record.schema.json`.

**Write classes** — `QUERY`, `RETRIEVAL`, `CLAIM`, `QUARANTINE`, `PACK`, `STATE`.

**Failure** — any mutation of a stored record changes its `record_hash` and breaks the chain at that
sequence number; `webis.cli ledger` reports `chain_valid: false, broken_at: n`. Verification never
repairs, and there is no delete operation.

**Acceptance** — `TestSKS8Ledger` (3 tests): chain verifies and ids are sequential; tampering is
detected at the exact record; a repeated write is deduplicated, not duplicated.

---

## SK-S9 · Untrusted-content quarantine

| | |
|---|---|
| Kind | [code] `Fetcher._scan_injection()` |
| Purpose | Ensure a page that addresses the agent cannot influence the agent, and that the attempt itself is reported |

**Inputs** — every retrieved body, before any extraction.

**Tools** — case-insensitive pattern set: `instruction_override`, `role_reassignment`,
`citation_forcing`, `omission_instruction`, `concealment_instruction`, `fake_system_block`.

**Outputs** — `FetchResult.injected = True` and `injection_evidence = [labels]`; the page is moved to
`quarantined_sources: {url, patterns, content_hash, reason}` and contributes **zero** passages.
Its presence sets pack `status: PARTIAL`.

**Evidence writes** — `QUARANTINE` record with the pattern labels and the body hash (the hash proves
*which* page was discarded, without storing the page).

**Why not just a denied domain** — `corpus/sites/citydesk_example/` is an allowed tier-3 domain.
Denying the domain would test the deny-list; the quarantine path needs a page that a policy would
happily fetch. (This is exactly the bug the test suite found: `viralbiz.example` was on the
deny-list and was refused *before* the scanner ran, leaving the quarantine branch dead code.)

**Failure** — anything else that looks instruction-like but does not match a pattern is still
subject to SK-S10/SK-S7: it can only enter the pack as a quotation that a reviewer reads verbatim.

**Acceptance** — `TestSKS9Quarantine` (3 tests): quarantine fires, all expected labels present, no
claim cites the page, `status: PARTIAL`; the domain is confirmed *allowed* so the scanner is what
fires; the scanner is case-insensitive and multi-pattern.

---

## SK-S10 · PII minimisation

| | |
|---|---|
| Kind | [code] `Fetcher.scan_sensitive()` |
| Purpose | Let the agent quote an adverse-media passage without propagating personal data |

**Inputs** — every passage before it is disclosed.

**Tools** — patterns for payment card (`payment_card`), passport-like (`passport_like`),
street address (`street_address`), DOB disclosure (`dob_disclosure`), bank detail (`bank_detail`).

**Outputs** — the quote with each matched span replaced by `[REDACTED]`; `pii_redactions: [kinds]`;
and `span_text_sha256` — the hash of the **original** span text.

**Why a hash instead of the text** — the auditor's question is "does this quotation really come from
that document at that offset?", which a hash answers without the PII ever being stored. Verification
therefore hashes `body[span]` and compares, and only requires literal equality between quote and span
when no redaction occurred. Without this, redaction would silently break `verify` — and did, before
the test suite caught it.

**Evidence writes** — `pii_redactions` and `span_text_sha256` on the citation inside the `CLAIM`
record.

**Acceptance** — `TestSKS10PII` (3 tests): values redacted and recorded; integrity proven by hash
rather than by the redacted text; a clean quote passes through untouched.

---

## SK-S11 · Budgets, checkpointing and resume

| | |
|---|---|
| Kind | [code] `SearchAgent.run()`, `snapshot()`, `restore()` |
| Purpose | Run for hours over hundreds of pages, survive interruption, and never pay twice for the same work |

**Inputs** — `config/budget.yaml` (`max_queries`, `max_fetches`, `max_domains`, `wall_clock_s`),
`checkpoint` (a prior `run-state`), `stop_after_queries` (caller-imposed ceiling, used by tests and
by schedulers that pre-empt long runs).

**Tools** — `snapshot()` (JSON-serialisable state), `restore()` (rebuild `SearchResult`, including
`FetchResult` and `Passage` objects), atomic write (`.tmp` + rename), URL-hash fetch cache, and
`on_checkpoint` callback wired to `out/state/run_<request_id>.json`.

**Outputs** — a `run-state` file after every completed query; on interruption,
`status: PARTIAL` and `gaps: [{gap: INTERRUPTED_BY_CALLER | FETCH_BUDGET_EXHAUSTED, detail}]`.

**Guarantees (all tested)**
* A checkpoint is only ever written from a consistent state: after a query completes, or while a
  query is mid-flight but every completed fetch is already in `res.fetches`.
* `query_index` = number of completed queries, so a resume **retries** the interrupted query rather
  than skipping it. Retrying is cheap: the cache prevents a second network fetch.
* A resumed run inherits the spent budget (`fetcher.fetches` is set from the restored state) — it
  does not get a fresh allowance.
* A run interrupted by `--stop-after N` and then resumed reaches the **same claims** as an
  uninterrupted run. This is the test that makes "resumable" a claim rather than an adjective.
* The state file is deleted only when a pack is successfully written.

**Acceptance** — `TestSKS11Resumption` (3 tests), plus the CLI round-trip in
`README.md § Quickstart` (`--stop-after 3` → `--resume`).

---

## Cross-cutting: what a skill is *not* allowed to do

Every skill above runs with no network primitives of its own except `Fetcher.fetch` (the single
egress point), no filesystem writes except through `EvidenceLedger` and the state file, and no ability
to change the source policy (`config/sources.yaml` is read-only at runtime, and the loader validates
it against `schemas/source-policy.schema.json`). A skill that needs a new capability needs a spec
change, not a code change in the model's prompt.
