# 05 · Long-running execution

A case here is not a prompt-and-answer. It reads hundreds of paginated pages, it waits ten business
days for a customer, and it comes back when new evidence arrives. This document is how the four
agents survive that: checkpoints, budgets, replay, idempotency, and the difference between a *wait
state* and a running process.

## 1. Three clocks

| clock | scale | what it means mechanically |
|---|---|---|
| **page clock** | 100s of pages, seconds each | OPS-PROFILE's acquisition is staged per endpoint and per page-set; a kill costs at most one stage |
| **call clock** | 10s of API calls, ms–s each | every call is charged against the run budget before it is made, and checkpointed after |
| **calendar clock** | days between RFI rounds | a pending customer reply is a *state* (`AWAITING_CUSTOMER`, `due_date`) with no process running; the agent is re-entered when the reply arrives |

Confusing the calendar clock with a running process is the classic failure: an agent that "waits" by
sleeping burns budget and cannot be deployed to a queue. Here, waiting writes a record and returns.

## 2. The stage model

A stage is a named unit of work with a payload. Stages are recorded in
`out/state/<CASE>-<AGENT>.json`:

```json
{
  "version": 1,
  "run_id": "RUN-51A361CE",
  "agent_id": "OPS-PROFILE",
  "case_id": "CASE-4831",
  "status": "COMPLETE",
  "head_record_hash": "sha256:…",
  "counters": {"api_calls": 11, "pages": 3, "checks": 0},
  "budget": {"api_calls": 400, "pages": 600, "checks": 200, "wall_clock_s": 900},
  "stages": {
    "c360": {"completed_at": "…", "agent": "OPS-PROFILE", "evidence_id": "OX-000001",
             "payload": {"body": {…}, "evidence_id": "OX-000001"}},
    "tx-flows": {"completed_at": "…", "payload": {"pages": [ … ], "evidence_ids": ["OX-000009"]}}
  }
}
```

Stage names, in order, for OPS-PROFILE:
`c360`, `related-parties`, `ownership`, `tx-summary`, `tx-flows`, `risk-rating`,
`jurisdiction-lists`, `documents`, `rfi-thread`, `counterparty-<id>` × n, `assemble`.
For OPS-VERIFY: `check:V-001` … `check:V-013`.

Three rules:

1. **A completed stage is never re-executed.** Its payload is replayed. That is what makes resume both
   cheap and *deterministic*: the resumed run sees the same bodies, so it computes the same
   `profile_id` and cites the same evidence.
2. **A completed stage is never re-charged.** Counters travel with the checkpoint, so a resumed run's
   budget accounting continues rather than restarting.
3. **A checkpoint pins the ledger head.** `head_record_hash` names the chain the checkpoint was written
   against; a checkpoint whose pin is no longer in the chain belongs to an archived ledger and is
   discarded rather than silently mixed with a new one.

Writes are atomic: `tmp` file + `os.replace`. A kill mid-write leaves the previous checkpoint intact,
never a truncated file.

## 3. Resume

```bash
make kill       # exits 70 (EX_SOFTWARE) on the 5th API call, after the last checkpoint is flushed
make resume     # replays c360..documents, fetches only what was outstanding, completes
```

Resume is the default: `cli profile` opens the checkpoint and continues. `--fresh` discards it. The run
summary records `resumed_from_checkpoint: true` and the profile's `data_quality.notes` says when no
live calls were made because everything came from the checkpoint.

What resume guarantees, tested:

| guarantee | test |
|---|---|
| the same `profile_id` and `evidence_id` as an uninterrupted run | `test_kill_and_resume_yields_the_same_profile_without_new_evidence` |
| no duplicate `API_CALL` records for replayed stages | same |
| the same numbers: every figure is derived from replayed bodies | `test_profile_matches_its_schema` on the resumed artefact |
| a checkpoint that leaves no `.tmp` file behind | `test_checkpoint_is_atomic_and_schema_valid` |

## 4. Budgets

| budget | default | what it protects |
|---|---|---|
| `api_calls` | 400 | the internal systems' rate limits and the cost of a runaway loop |
| `pages` | 600 | pagination loops (the classic "next_page never null" bug) |
| `checks` | 200 | repeated verification of an unchanged bundle |
| `wall_clock_s` | 900 | a hung connector |

`RunState.check_budget()` raises **before** the work: a budget that only reports is a budget that
cannot stop anything. On `BudgetExceeded` the agent:

1. marks the run `BUDGET_EXHAUSTED` and saves the checkpoint,
2. emits no partial number (a half-computed HHI is worse than no HHI),
3. returns `PARTIAL` naming what is missing,
4. can be resumed with a larger budget without re-doing the stages it completed.

`make budget` demonstrates it with `--budget 4`.

## 5. Idempotency and re-entry

Every agent is safe to re-run on the same inputs: deterministic ids, an idempotency store, and
content-addressed keys (see `04-evidence-writes.md`). Re-entry points by agent:

| agent | re-entry trigger | what it re-does |
|---|---|---|
| OPS-PROFILE | new `as_of`, new documents, scheduled refresh | full rebuild + `PROFILE_DELTA` against the previous profile |
| OPS-VERIFY | any artefact changed | full 13-check battery (checks are cheap; a stale verdict is not) |
| OPS-OUTREACH | customer reply, deadline passed, approval granted | `assess()`, follow-up, or `send()` — never a silent resend |
| OPS-DECIDE | new profile, new verification, RFI assessment | new envelope, new disposition, `diff()` against the previous one |

The RFI thread is the calendar-clock state machine: `READY_TO_SEND → PENDING_APPROVAL → SENT →
RESPONSE_ASSESSED`, with `REFUSED_ROUNDS_EXHAUSTED` as a terminal state. `rfi_max_rounds` comes from
the policy pack, so raising the number of allowed rounds is a policy change, not an agent change.

## 6. Scale

The mock API paginates `/tx/flows` at 100 rows per page; a busy corporate is 300–800 pages over a
90-day window. At 400 pages the profile makes 400 `API_CALL` records and one `PROFILE` record, and the
page budget is the only thing that stops it. `scripts/make_large_case.py` generates a synthetic
400-page case (`mock/api/large_case/`) so the pagination path, the budget stop and the resume can be
exercised without pretending a three-page fixture is scale:

```bash
make large-case      # generate the fixture (400 pages, ~40k transactions)
make scale           # run with a 120-call budget -> PARTIAL, then resume -> COMPLETE
```

## 7. Concurrency and honesty about limits

* **One writer per (case, agent).** The checkpoint is keyed on that pair; two processes writing the
  same checkpoint would interleave. Serialising agents per case is the orchestrator's job; this package
  does not implement a distributed lock, and does not pretend to.
* **The ledger is append-only, line-buffered, and process-locked.** Concurrent writers append safely,
  but `seq` is per-process state, so a multi-process deployment must allocate sequence blocks or use a
  store that does. In this single-process reference implementation, `seq` is continuous and verified.
* **Verification does not stream.** A 13-check battery over a 2 MB profile is a second of work; it is
  checkpointed for restartability, not for throughput.
* **What is genuinely long-running is the calendar**, and it is modelled as state, not as time.
