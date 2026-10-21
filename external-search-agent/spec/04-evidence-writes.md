# 04 · Evidence writes

The search agent's output is only as good as the record of how it was produced. This document
specifies every write: what is written, when, into which store, with which keys, and what happens
when the same work is attempted twice.

## 1. Two stores, two purposes

| Store | Path | Purpose | Mutability |
|---|---|---|---|
| **Hash-chained ledger** | `out/search_evidence.jsonl` | the legal record: what was asked, what was fetched, what was claimed, in order | append-only, tamper-evident |
| **Artefacts** | `out/evidence_pack.json`, `out/state/*` | the working products: the hand-off and the resume point | rewritten freely; both are reproducible from the ledger + source hashes |

The ledger answers *"can you prove you did this, and in this order?"*. The pack answers *"what do you
know?"*. Neither is derived from the other at read time, so a corrupted cache cannot rewrite history
and a lost ledger cannot silently change a pack (the pack carries its own hashes).

## 2. Write classes

| `record_type` | Written by | Trigger | Key payload |
|---|---|---|---|
| `QUERY` | SK-S1 | every planned query, once | `query_id, text, facet, rationale` |
| `RETRIEVAL` | SK-S3 | every URL considered — including refusals | `url, status, content_hash?, reason?` |
| `CLAIM` | SK-S7 | every survivorship claim | `claim_key, claim{text,confidence,corroboration_count}, citations[{evidence_id,url,quote,char_span,content_hash}], contradictions[]` |
| `QUARANTINE` | SK-S9 | every quarantined page | `url, patterns[], content_hash` |
| `PACK` | SK-S7 | once per completed run | `pack_id, pack_hash, status, claim_count, quarantined, refused, gaps, cost` |
| `STATE` | SK-S11 | resume, budget stop, caller interruption | `event, detail` |

## 3. Record anatomy

```json
{"evidence_id": "WS-000017", "agent_id": "WEBIS-SEARCH", "seq": 17,
 "created_at": "2026-10-21T12:00:04Z", "record_type": "CLAIM",
 "request_id": "SR-0001", "claim_key": "corporate_status",
 "claim": {"text": "The subject has entered administration.", "confidence": "LOW",
           "corroboration_count": 1},
 "citations": [{"evidence_id": "EV-350ffdd34293",
                "url": "https://commoditytoday.example/news/meridian-administration",
                "quote": "Commodity trader Meridian Trade Holdings Ltd has entered administration, …",
                "char_span": [0, 139], "content_hash": "sha256:…"}],
 "contradictions": [],
 "prev_hash": "sha256:…", "record_hash": "sha256:…", "signature": "kms-stub:…"}
```

`evidence_id` (`WS-nnnnnn`) is the ledger's own sequence — the handle a reviewer cites when they say
"show me the fourth source you fetched". `EV-…` ids belong to quotations and are content-derived
(`sha256(url + quote)`), so the same quotation keeps its id across runs and packs; that is what makes
"this claim was already made in the March pack" a checkable statement.

## 4. When a write happens (ordering guarantees)

```
plan ──► QUERY × n
for each query:
    for each hit:
        RETRIEVAL            (before the body is used, so a crash after fetch is still recorded)
        QUARANTINE  or  refusal/RETRIEVAL
    checkpoint                (state file; not a ledger record)
claim formation ──► CLAIM × n
pack assembly   ──► PACK × 1
```

Consequences worth stating:

* A fetch is recorded **before** its content influences anything. A run killed mid-extraction still
  shows what was fetched.
* Claims are written after all retrieval for the run, because a claim's corroboration count depends
  on the whole run. A partial run writes no claims — it writes a `STATE` record and a checkpoint, and
  its pack (if rendered) is `status: PARTIAL` with `gaps`.
* `PACK` is written last and carries `pack_hash`, the digest of the entire pack. The pack file is the
  only artefact whose hash appears in the ledger, so the chain always ends at something a reviewer
  can open.

## 5. Idempotency

Every write takes a key:

| Class | Key |
|---|---|
| QUERY | `query \| request_id \| query_id` |
| RETRIEVAL | `retrieval \| request_id \| url \| status` |
| CLAIM | `claim \| request_id \| claim_key \| canonical(citations)` |
| PACK | `pack \| request_id \| pack_hash` |

`IdempotencyStore` (`out/state/idem.json`) maps key → `evidence_id`. A repeated write returns the
existing id and appends nothing. This is what makes resume safe: re-running the interrupted query
re-derives the same QUERY records, and the ledger does not grow a duplicate history. A *changed*
citation set changes the key, which is correct — a claim whose evidence changed is a new claim, and
both versions stay in the ledger.

## 6. Tamper evidence

```
record_hash = sha256( canonical( record − {record_hash, signature} ) )
prev_hash   = previous line's record_hash        (first line: "GENESIS")
signature   = "kms-stub:" + record_hash[7:23]    (placeholder for a KMS/HSM signature)
```

`canonical()` = JSON with sorted keys and no whitespace, so hashing does not depend on formatting.
`EvidenceLedger.verify_chain()` folds the file and returns `(ok, head, broken_at)`.

Tested behaviour (`TestSKS8Ledger`): editing the text of record 1 in a two-record file yields
`chain_valid: false, broken_at: 1`. The verifier never repairs; a broken chain is an incident, not a
cleanup task.

Known limitation, stated rather than hidden: `kms-stub` is a stub. A real deployment signs
`record_hash` with a key the agent cannot read, so that "the agent wrote this" is verifiable by a
third party. The chain structure and every test around it are unchanged by that swap.

## 7. Retention and minimisation in the record

* **No full text in the ledger.** `CLAIM` records carry quotations (the minimum needed to be
  checkable) and hashes, never whole documents. `store_full_text: false` in the source policy is a
  `const` in the schema, so a deployment cannot quietly turn the ledger into a copy of the web.
* **Redaction applies to the ledger too.** A quotation in a `CLAIM` record is the redacted text plus
  `span_text_sha256` of the original span; the ledger never holds the card number it has proven the
  existence of.
* **A quarantined page's content is never stored** — only its URL, its pattern labels and its body
  hash. The record says "we discarded a page that tried to instruct us", which is the useful fact,
  without ingesting the payload.
* **Refusals are retained with reasons.** `BLOCKED_DOMAIN`, `ROBOTS_DISALLOWED`, `PAYWALLED`,
  `EXCLUDED_SAME_NAME` — the reason is what distinguishes "we could not" from "we would not", and
  both are asked about in review.

## 8. What the CDD agent receives

The CDD agent does not read the ledger. It reads the pack and attaches quotations by `evidence_id`
into its own findings, which are in turn signed by AXIOM-POLICY decisions. The chain of custody is
therefore: `WS-nnnnnn` (search ledger) → `EV-…` (quotation) → CDD finding → `PZ-nnnnnn` (policy
decision). Each hop is a hash or a foreign key; no hop is "the model said so".
