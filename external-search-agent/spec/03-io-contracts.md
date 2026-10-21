# 03 · I/O contracts

Six schemas, all JSON Schema 2020-12, all in `schemas/`, all validated in CI
(`TestContracts` in the acceptance suite). Nothing crosses an agent boundary that is not one of
these shapes.

| Schema | Direction | Validated where |
|---|---|---|
| `search-request.schema.json` | in | CLI, `SearchAgent.run` precondition |
| `passage.schema.json` | internal | every `Passage` before it can be cited |
| `evidence-pack.schema.json` | **out (the hand-off)** | `pack()`, and again by the consumer on receipt |
| `evidence-record.schema.json` | out (ledger line) | every append in `EvidenceLedger` |
| `run-state.schema.json` | state | every checkpoint write, and every restore |
| `source-policy.schema.json` | config | loader; a malformed policy refuses to start |

---

## 1. SearchRequest

```json
{
  "request_id": "SR-0001",
  "subject": "Meridian Trade Holdings Ltd",
  "case_id": "CASE-4831",
  "attributes": {"dob": "1979-03-14", "country": "VE", "registration_number": "VE-8841-22"},
  "related_parties": ["Elena Vasquez", "Viktor Sorensen"],
  "facets": ["IDENTITY", "ADVERSE", "REGULATORY", "CORPORATE_STATUS", "RELATED_PARTY"],
  "max_queries": 18,
  "per_query_limit": 4,
  "retrieved_at": "2026-10-21T12:00:00Z"
}
```

Rules that the contract enforces rather than trusts:

* `request_id` matches `^SR-[0-9]{4,8}$` — it is also the idempotency namespace for the ledger and
  the filename of the checkpoint. A caller that reuses an id gets resume behaviour, not a second run.
* `attributes` is the *only* channel through which a namesake can be excluded. Absent attributes
  never exclude anything (SK-S5b: absence is not a conflict). If a caller omits `dob`, the pack will
  contain a note saying the search could not discriminate on date of birth.
* `related_parties` are screened **under their own names**. Passing them in is not cosmetic: a UBO's
  adverse media is frequently invisible through the entity.
* `retrieved_at` is mandatory in practice (the CLI defaults it, the agent never reads a wall clock).
  Two runs with the same request and the same `retrieved_at` produce identical claims — tested.
* `additionalProperties: false` — there is no free-form "notes" field for a caller to smuggle
  instructions into the record.

## 2. Passage (internal)

```json
{
  "passage_id": "PSG-01",
  "url": "https://fca.org.uk/news/final-notices/2024/meridian-trade-holdings",
  "quote": "The Financial Conduct Authority has published a final notice in respect of …",
  "char_span": [0, 88],
  "content_hash": "sha256:…",
  "source_tier": 1,
  "published": "2024-11-02",
  "retrieved_at": "2026-10-21T12:00:00Z"
}
```

The load-bearing invariant: **`body[char_span[0]:char_span[1]] == quote`**. `quote.minLength` is 40
characters — a fragment shorter than that cannot carry a fact without its context.

## 3. EvidencePack — the hand-off

```
pack_id                     PACK-<12 hex>  (sha256 over the pack contents; identical input → identical id)
request_id, subject, status OK | PARTIAL | FAILED
source_policy_version       e.g. 1.0.0
method                      {agent, agent_version, system_prompt_sha256, model_routing, retrieved_at}
query_log                   the planned queries, facet and rationale
claims[]                    see below
quarantined_sources[]       {url, patterns[], content_hash, reason}
refused_sources[]           {url, status, reason, relevant?, quote?}
unverified_circulation[]    {url, status, note}
gaps[]                      {gap, detail}  — PAYWALLED_SOURCE | FETCH_BUDGET_EXHAUSTED |
                                             INTERRUPTED_BY_CALLER | NO_RESULTS | LANGUAGE_UNSUPPORTED
cost                        {queries, fetches, domains}
```

Each claim:

```
claim_key, claim, confidence (HIGH|MEDIUM|LOW|UNSUPPORTED), citable
corroboration_count      number of *independent origins*
syndication_detected     domains > origins
independent_domains[]    domains that carried the passage
origins{}                origin → the domains that reached it
notes[]                  syndication / unretrieved origin / contradiction — reviewer-facing
contradictions[]         passages that assert the opposite
newest_source_age_days, refresh_limit_days, refresh_due
citations[]              ≥ 1, see below
```

Each citation:

```
url, quote, quotes[], char_span, content_hash, span_text_sha256
published, source_tier, pii_redactions[]
credibility {score, band, factors[{factor, value, points}]}
evidence_id (EV-…), evidence_ids[]  — one entry per URL; extra passages ride along
```

**Contract-level guarantees**
1. `citations` has `minItems: 1`. A citation-less claim is not representable — this is a schema
   failure, not a policy one.
2. One citation entry per URL. Three passages from one document must not look like three sources.
3. `content_hash` is the hash of the whole body as retrieved; `span_text_sha256` is the hash of the
   span's original text (survives PII redaction). `verify` recomputes both.
4. `confidence` derives only from the citations and the origin count — never from model judgment.
5. Consumption rule for downstream agents: **read `claims[]`, `refused_sources[]`, `gaps[]` and
   `unverified_circulation[]` together.** A pack read as "claims only" overstates certainty; a pack
   read as "no claims means no adverse media" is simply wrong — it means *no citable adverse media
   was found within this search, scope stated in `cost` and `gaps`*.

## 4. EvidenceRecord — the ledger line

```
seq, evidence_id (WS-nnnnnn), agent_id, created_at, record_type
record_type ∈ QUERY | RETRIEVAL | CLAIM | QUARANTINE | PACK | STATE
prev_hash, record_hash, signature
+ the type-specific payload (query / url+status / claim / pack_id+pack_hash / event+detail)
```

`record_hash = sha256(canonical(record − {record_hash, signature}))`, `prev_hash` = the previous
line's `record_hash` (genesis = `GENESIS`). Verification is a fold over the file: any edit to any
line, or any removal, is detected and reported at the first divergent `seq`.

## 5. RunState — the checkpoint

```
schema: "webis.run-state/1"
request_id, subject, status, query_index
queries[]            the plan, so a resume does not need to re-plan
fetches{url: FetchResult}    bodies included: a resume must not re-fetch
passages[]           already-extracted passages, spans intact
quarantined[], refusals[], gaps[], cost{}
```

`query_index` = number of *completed* queries. Resume re-enters at that index, so an interrupted
query is retried — safe, because `fetches` is restored and the URL cache answers instantly.

## 6. SourcePolicy — configuration

Validated shape of `config/sources.yaml`; `max_tier_citable: 3`, tiers 1–5 with `weight` and
`citable_alone`, `denied_domains`, a `compliance` block whose four booleans are `const`:
`obey_robots: true`, `honour_paywalls: true`, `honour_licensing: true`, `store_full_text: false`.
That is not a default that a deployment can flip: a policy file that sets `honour_paywalls: false`
fails validation and the agent does not start.

## 7. Versioning

* Breaking schema change → `evidence-pack/2.0.0`, new `pack_id` namespace; consumers must opt in.
* Additive field → minor bump; consumers must ignore unknown fields (they are `additionalProperties:
  false` for *producers*, which is what makes the contract tight, and unknown fields are rejected on
  read by the consumer's own copy of the schema).
* `source_policy_version` and `system_prompt_sha256` are stamped into every pack so that a change in
  behaviour can always be traced to a change in configuration or prompt.
