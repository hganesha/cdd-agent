# 07 · Evaluation

The agent is evaluated on four axes, in this order of importance: **integrity** (can we prove what it
says), **calibration** (does it know how strong its own evidence is), **recall** (did it find what was
findable), **honesty about scope** (does it tell us what it missed). Accuracy of any specific factual
claim about a real subject is a fifth axis that cannot be measured on a synthetic corpus — see §6.

## 1. Integrity — pass/fail, no partial credit

| Metric | Definition | Target | Where |
|---|---|---|---|
| Quote precision | fraction of quoted passages that re-slice byte-identically from a document whose hash matches | **100%** | `webis.cli verify` |
| Citation coverage | fraction of claims with ≥ 1 valid citation | **100%** | schema + `TestSKS7CitationPack` |
| Span drift | maximum character offset by which any quote disagrees with its span | **0** | `test_every_quote_is_byte_identical_to_its_span` |
| Chain validity | ledger verifies end to end | **true** | `webis.cli ledger` |
| Tamper detection | injected edit detected at the right sequence number | **detected** | `test_tampering_with_a_record_breaks_the_chain_at_that_record` |

A single failed quote invalidates the pack: `verify` exits 2 and the pack must not be consumed. This
is not a threshold — a quotation that cannot be re-derived is a fabricated quotation, whether or not
anyone intended it.

## 2. Calibration — corroboration must mean what it says

| Metric | Definition | Target | Corpus case |
|---|---|---|---|
| Syndication collapse | a fact published by N outlets from one origin counts as 1 | **1** | `globalwire` → `commoditytoday` (and the reverse direction in tests) |
| Unretrieved-origin disclosure | an attribution to an un-fetched origin produces a note | **100%** | `commoditytoday` attributing to Global Wire |
| Independent pair | two genuinely independent outlets count as 2 | **2** | `fca.org.uk` + `financialgazette` |
| Tier discipline | a single tier-3 source cannot reach `HIGH` | **never HIGH** | `commoditytoday` alone |
| Contradiction surfaced | opposing passages appear in `contradictions[]`, not averaged | **all pairs** | injection page vs FCA notice (after quarantine, none arise) |

Calibration is measured as *agreement between the reported corroboration count and the number of
distinct origins*, not as a probability. That is a mechanical property and therefore testable.

## 3. Recall — did we find what was there to find

Run against the fixture corpus with a ground-truth list of 9 planted facts:

| # | Fact | Expected outcome | Why |
|---|---|---|---|
| 1 | FCA final notice, penalty £1,240,000 | claim, `HIGH`, 2 origins | tier 1 + tier 2 |
| 2 | Conduct negligent, not dishonest | claim, `MEDIUM` | single tier-1 passage |
| 3 | Administration reported by Global Wire | claim, `LOW`, 1 origin, syndication note | two outlets, one origin |
| 4 | Customs inquiry, no charges | claim, `LOW` | tier 2 |
| 5 | EUR-Lex sanctions entry (lookalike name) | refused as same-name | SK-S5b |
| 6 | FT analysis of the administration | gap `PAYWALLED_SOURCE` | paywall |
| 7 | "Internal documents" dump | refused `BLOCKED_DOMAIN` | deny-list |
| 8 | Laundering allegation on an aggregator | `unverified_circulation` only | tier 4 |
| 9 | Same-name namesakes ×3 (DOB, age, location) | 3 × `EXCLUDED_SAME_NAME` | SK-S5b |

**Recall target: 9/9 accounted for.** Not 9/9 *found* — facts 5–9 must be *accounted for* as refused,
quarantined or circulating. Recall of the negative evidence matters as much as recall of the positive:
a pack that reports 4 claims and omits the fact that it refused 3 sources is worse than useless.

## 4. Honesty about scope

| Metric | Definition | Target |
|---|---|---|
| Gap coverage | every paywall, budget stop and quarantine appears in `gaps[]` / `quarantined_sources[]` | **100%** |
| Refusal reasons | every non-OK status carries a machine-readable reason | **100%** |
| Status truth | `PARTIAL` iff something was cut short | **exact** |
| Cost truth | `cost.fetches` equals the number of RETRIEVAL records in the ledger | **exact** |
| Failure honesty | an unsearchable subject returns `FAILED` + `NO_RESULTS`, never a thin `OK` | **exact** |

## 5. Operational metrics

| Metric | Target | Note |
|---|---|---|
| Determinism | two runs, same `request_id` + `retrieved_at` → identical `claims` | tested; model temperature 0 and code-only scoring |
| Resume equivalence | interrupted + resumed == uninterrupted, in claim keys | tested |
| Budget adherence | fetches ≤ `max_fetches`, domains ≤ `max_domains` | tested per fetch |
| Wall clock | 40 fetches under 900 s with the 1 s domain delay | `max_wallclock_s` |
| Cost per request | ≤ $6.00 (planning + phrasing only) | `max_usd` |
| Ledger growth | ~1 QUERY + 1 RETRIEVAL per URL + 1 CLAIM per claim + 1 PACK | bounded, no full text |

## 6. What this suite cannot tell you

Honest limitations, because a compliance artefact that overstates its own validation is a liability:

1. **The corpus is synthetic.** `fca.org.uk` fixtures are written for this repository. Precision and
   recall against the live web are unknown and must be established by a shadow-mode pilot with
   human-labelled packs, not by this suite.
2. **The injection corpus contains six pattern families.** A real adversary writes in other styles.
   The guardrail's strength is architectural (quarantine + citation-only output + human review), not
   lexical completeness.
3. **Same-name discrimination is heuristic.** DOB/age/location are the discriminators the corpus
   contains; a namesake with the same DOB and country is not distinguishable by these rules and must
   be resolved by a human.
4. **Credibility weights are policy choices, not measurements.** They are visible, reproducible and
   reviewable — which is the property that matters — but they are not empirically calibrated.
5. **The KMS signature is a stub.** Chain structure holds; third-party non-repudiation does not until
   a real key is used.

## 7. Regression protocol

```bash
make check            # compile, schema validation, 54 acceptance tests, verify, ledger
make demo             # the reference run, printed; human-readable pack
make diff-pack        # compare out/evidence_pack.json against out/baseline/evidence_pack.json
```

Any change to `config/sources.yaml`, `prompts/*`, or a scoring constant requires: (a) all tests green,
(b) an updated baseline pack with a written justification of *each* claim whose confidence moved, and
(c) a note in the pack's `method` block via the prompt hash and policy version. A run-to-run diff that
is not explained is treated as a defect, not an improvement.

## 8. The acceptance suite (54 tests)

| Class | Tests | Skill |
|---|---|---|
| `TestSKS1QueryPlanning` | 4 | SK-S1 |
| `TestSKS2SourcePolicy` | 4 | SK-S2 |
| `TestSKS3Compliance` | 5 | SK-S3 |
| `TestSKS4PassageExtraction` | 3 | SK-S4 |
| `TestSKS5Credibility` | 6 | SK-S5 |
| `TestSKS5bSameName` | 5 | SK-S5b |
| `TestSKS6Triangulation` | 3 | SK-S6 |
| `TestSKS7CitationPack` | 6 | SK-S7 |
| `TestSKS8Ledger` | 3 | SK-S8 |
| `TestSKS9Quarantine` | 3 | SK-S9 |
| `TestSKS10PII` | 3 | SK-S10 |
| `TestSKS11Resumption` | 4 | SK-S11 |
| `TestContracts` | 5 | schemas / I-O |

Every test builds its own corpus in a temporary directory, so a change to `corpus/sites/` cannot make
a test pass or fail by accident. The reference corpus is only used by the two tests that assert the
whole-corpus span invariant and the tier resolution — i.e. the tests that are *about* the corpus.
