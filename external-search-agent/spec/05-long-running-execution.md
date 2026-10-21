# 05 · Long-running execution

A subject with a common name across three jurisdictions is not one search. It is 18 queries, 40
fetches, a paywall, a quarantine, three namesakes and a regulator's site that times out on the second
attempt — spread over tens of minutes, sometimes over hours once rate limits are respected. This
document specifies how the agent survives that without losing work, double-spending budget, or lying
about what it finished.

## 1. The unit of work, and the unit of safety

The unit of work is **a query**, because that is the smallest step whose results are mutually
independent. Within a query, individual fetches are also idempotent (URL-hash cache), so a crash mid-
query costs at most the un-cached fetches of that query.

```
query 1 ──► fetch a, b, c ──► passages ──► CHECKPOINT (query_index = 1)
query 2 ──► fetch d, e    ──► passages ──► CHECKPOINT (query_index = 2)
     … crash …
resume at query_index = 2  → re-runs query 2, hits the cache for d and e, continues
```

## 2. Checkpoint contents

`out/state/run_<request_id>.json`, validated against `schemas/run-state.schema.json`:

| Field | Why it must be there |
|---|---|
| `query_index` | the only resume cursor; the number of *completed* queries |
| `queries[]` | so a resume does not re-plan (and cannot re-plan differently after a prompt change) |
| `fetches{url: FetchResult}` | bodies included — a resume must not pay for the same page twice |
| `passages[]` | extraction results, spans intact, so no re-extraction |
| `quarantined[]`, `refusals[]` | the negative evidence is not re-derived either |
| `gaps[]`, `cost{}` | so a resumed run reports the whole run's cost, not the last leg's |

**Write atomicity**: the file is written to `run_<id>.json.tmp` and `os.replace`d. A kill between the
two leaves the previous checkpoint intact; there is no state in which the file is half-written and
parseable-as-wrong.

## 3. Resume semantics (five guarantees, all tested)

1. **No lost work.** `restore()` rebuilds `SearchResult` including `FetchResult` and `Passage`
   objects, so every fetch already paid for is present after resume.
2. **No skipped work.** `query_index` counts *completed* queries, so the interrupted query is retried
   rather than assumed done. Retrying costs nothing: the URL cache answers.
3. **No fresh budget.** `restore()` sets `fetcher.fetches = max(fetcher.fetches, len(res.fetches))`.
   A resumed run inherits the spend; `max_fetches` remains a ceiling on the run, not on the leg.
4. **No duplicate evidence.** Ledger keys are per `(request_id, query_id)` etc., so re-running a query
   appends nothing (SK-S8 §5).
5. **Same answer.** A run interrupted by `--stop-after N` and resumed produces the same claims, in
   the same order, as an uninterrupted run. `TestSKS11Resumption` asserts exactly this; without it,
   "resumable" would be a claim about mechanics rather than about outcomes.

## 4. Budgets

From `config/budget.yaml`:

| Ceiling | Value | Enforced at | Behaviour |
|---|---|---|---|
| `max_queries` | 18 | plan time (hard cap on `plan_queries`) | fewer queries, no truncation of results |
| `max_fetches` | 40 | **per fetch**, inside the hit loop | `FETCH_BUDGET_EXHAUSTED`, `status: PARTIAL`, checkpoint written |
| `max_domains` | 15 | retrieval | stop adding new domains; existing ones continue |
| `max_wallclock_s` | 900 | between queries | treated exactly like a caller interruption |
| `max_usd` | 6.0 | model calls (planning + phrasing only) | phrasing falls back to the canonical sentence for the `claim_key` |
| `soft_stop_pct` | 80 | as each fetch lands | the agent stops *starting* new domains, finishes what it has |

Two properties matter more than the numbers:

* **The budget binds per fetch, not per query.** One query can surface four URLs; checking the
  ceiling only at query boundaries lets a run overshoot by up to `per_query_limit` fetches. This was
  a real bug found by `TestSKS3Compliance` and fixed by moving the check inside the loop.
* **Exhaustion is reported, never hidden.** A budget-stopped run is `PARTIAL` with a named gap, and
  its pack is still valid and still useful — the claims it did establish are as good as they would
  have been. What it must not do is present itself as complete.

## 5. Interruption taxonomy

| Cause | `status` | `gaps[]` | State file |
|---|---|---|---|
| All queries done | `OK` | — | deleted |
| Quarantined page present | `PARTIAL` | — (`quarantined_sources` is the signal) | deleted |
| Fetch budget | `PARTIAL` | `FETCH_BUDGET_EXHAUSTED` | kept |
| Caller/scheduler stop (`--stop-after`, SIGTERM) | `PARTIAL` | `INTERRUPTED_BY_CALLER` | kept |
| Paywall encountered | `OK` or `PARTIAL` | `PAYWALLED_SOURCE` | deleted when complete |
| Subject unsearchable | `FAILED` | `NO_RESULTS` | deleted |
| Crash / kill -9 | (no pack written) | — | last good checkpoint survives |

`FAILED` vs `PARTIAL` is a deliberate distinction: `PARTIAL` means "this answer is true but
incomplete"; `FAILED` means "there is no answer here, and here is what would be needed to get one".

## 6. Operating a long run

```bash
# a scheduler that must not exceed a 10-minute slice
python3 -m webis.cli search --subject "…" --stop-after 6 ; echo $?   # 3 = interrupted
python3 -m webis.cli search --subject "…" --resume    ; echo $?      # 0 = pack written

# after a crash, no special handling: the checkpoint is already the last completed query
python3 -m webis.cli search --subject "…" --resume
```

`--resume` is explicit rather than implicit: silently continuing an old run when the caller expected
a fresh one is how a stale pack gets delivered as today's answer. The state file is the same request
id's, so the caller's `request_id` determines the identity of the run.

Operational hygiene:

* `out/cache/` is content-addressed and safe to keep across runs; deleting it only costs time.
* `out/state/idem.json` must survive with the ledger — deleting it makes the next resume append
  duplicate records (harmless for truth, corrosive for audit).
* The state file is per `request_id`; two concurrent runs of the same request id would race. The CLI
  takes a lock file (`out/state/run_<id>.lock`) and refuses a second writer.

## 7. Scale notes (what changes at 100s of pages)

* **Query fan-out, not concurrency, is the lever.** 40 fetches is one request's budget; a severe-case
  subject gets several request ids (`SR-0001`, `SR-0001-a` for the local-language pass), each with its
  own checkpoint, and the CDD agent merges packs by `evidence_id`.
* **Breadth-first by construction.** Passages are extracted per document immediately, so a resume
  never needs the raw body of a fully-processed page; `passages[]` in the checkpoint is small.
* **Checkpoint size is bounded** by `queries × per_query_limit` documents, because `fetches` holds one
  entry per URL, not per passage. At 40 documents the state file is tens of kilobytes.
* **The ledger grows monotonically** and is the thing to ship to cold storage; the pack stays small
  because citations are single quotes with hashes, not documents.
