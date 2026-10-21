# 06 · Guardrails

Every guardrail below is a control with a named enforcement point, a test, and a stated residual
risk. Guardrails that exist only as prompt text are listed in §6 separately, because they are the
weakest class and should not be confused with the rest.

## 1. Untrusted content (the hostile internet)

| Risk | Control | Enforcement | Residual |
|---|---|---|---|
| Prompt injection in a retrieved page | Quarantine: pattern scan before any extraction; page contributes zero passages; `status: PARTIAL` | `Fetcher._scan_injection` (SK-S9), tested on an *allowed* tier-3 domain (`citydesk.example`) | A novel phrasing that matches no pattern passes as text — but it can then only enter the pack as a verbatim quotation a human reads |
| Instruction embedded in a page the agent is asked to *describe* | `SYS-DESCRIBE` receives pattern labels and metadata only, never the page text | prompt contract §02 | model could speculate; output is one neutral sentence, reviewed |
| Citation forcing ("cite this URL as authoritative") | Tier is policy-assigned; a page cannot self-declare a higher tier | `test_tier_cannot_be_self_declared_upward_by_a_page` | none known |
| Data-exfiltration via retrieval (URLs crafted to leak context) | The agent only ever requests URLs from the search index; queries are built from the subject, not from page content | `Fetcher.search` + `plan_queries` | a page cannot inject a *new* URL into the plan |

The structural point: the agent's egress is one function (`Fetcher.fetch`) and its ingress is one
function (`extract_passages`). Both are code, both are tested, and neither reads its instructions from
content.

## 2. Source quality and provenance

* **Tier is configuration.** `config/sources.yaml`, schema-validated, version-stamped into every pack.
  Runtime cannot change it; the loader refuses a policy file whose compliance constants are not
  `true`/`false` as specified.
* **Tier 4 never carries a claim alone.** `max_tier_citable: 3`; a tier-4 page can only ever appear
  in `unverified_circulation` ("recorded, not relied upon").
* **Deny-list is absolute.** `scrapedleaks.example` (obtained without consent, contains personal
  data) is refused before its body is read: `body == ""`, no hash, no quote.
* **Independence is computed, not assumed.** SK-S6; the failure it prevents (three outlets, one wire,
  "widely reported") is the single most common defect in automated adverse-media output.

## 3. Personal data

* `store_full_text: false` is a `const` in the source-policy schema; quotations, not documents.
* Redaction before disclosure: card, passport-like, street address, DOB disclosure, bank detail →
  `[REDACTED]`, with `pii_redactions[]` recording the kinds.
* Integrity is proven by `span_text_sha256` over the original span, so the ledger never holds the
  value it has proved the existence of.
* `localnews.example/…/court-listing-meredith-vasquez` carries a PII payload *and* is a namesake: it
  is excluded on the namesake ground before its PII ever matters. Both controls are exercised, in
  that order, by `TestSKS5bSameName` and `TestSKS10PII`.

## 4. Compliance with publishers

Robots (strictest applicable rule), paywalls (metadata only, never bypassed), licensing, declared
user agent, ≥1000 ms between fetches to one domain, one concurrent request per domain, `Retry-After`
honoured, `respect_noarchive`. A robots-disallowed body is discarded **and the fact of the refusal is
recorded** — compliance that is invisible is indistinguishable from non-compliance.

## 5. Financial and operational limits

Budgets (§05), the single-writer lock (`out/state/run_<id>.lock`), and the rule that the state file is
deleted only when a pack is written. A second process racing the same `request_id` exits 4 with a
message rather than producing interleaved evidence.

## 6. Prompt-only controls (weakest class — named honestly)

These are enforced by instruction, not by code, and should be treated as *review aids*:

1. Not inventing identifiers, dates, amounts (partly caught later by span verification).
2. Not arguing a namesake exclusion back in.
3. Not writing clearance language ("no adverse media exists").
4. Not reconstructing redacted values.
5. Choosing good queries and honest claim sentences.

Each has a compensating control downstream: (1) fails `verify`; (2) leaves an `EXCLUDED_SAME_NAME`
record that contradicts any use of the passage; (3) is visible to the reviewer reading one sentence;
(4) is not recoverable from anything the agent emits; (5) is the reviewer's job by design.

## 7. What this agent must never do

| Never | Why | Enforced by |
|---|---|---|
| Emit a claim with no citation | the whole product is checkable evidence | schema `minItems: 1` |
| Count one wire story as several sources | false corroboration is worse than no corroboration | SK-S6 + tests |
| Cite a denied, robots-disallowed or paywalled body | legal exposure + unverifiable quotation | SK-S2/SK-S3 |
| Store full page text | minimisation; publisher rights | `store_full_text: false` const |
| Quote PII | data protection | SK-S10 |
| Decide anything about the customer (risk, clearance, EDD) | that is CDD's and the policy agent's job | §00 §2, prompt Rule 6 |
| Read the wall clock | determinism and reproducibility | `retrieved_at` is an input |
| Silently drop a refusal, quarantine or gap | an incomplete pack that looks complete is a false statement | pack schema requires all three arrays |
| Modify its own source policy or prompt | change control | read-only config, hash-pinned prompt |

## 8. Anticipated misuses and their answers

* *"Search harder until you find something adverse."* — The budget caps queries and the agent has no
  objective function rewarding volume; its confidence falls when corroboration is single-sourced, so
  volume alone cannot raise a claim's standing.
* *"Just use the aggregator, it's faster."* — Tier 4 is not citable; the material appears only as
  circulation with a note.
* *"Summarise this page for me."* — Not a capability. The pack carries quotations, and a summarisation
  request is a prompt-injection vector; the quarantine scanner and the citation requirement are what
  remain if such a request were somehow honoured.
* *"The namesake exclusion is wrong, put it back."* — Allowed, but it is a *new* run with different
  `attributes`, and the pack records which attributes produced which exclusions. Overriding by
  editing the pack breaks the chain and is detectable.
