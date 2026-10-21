# 04 · Evidence writes

One ledger. Four writers. Every claim in every artefact points at a record in
`out/ops_evidence.jsonl`.

```
record_hash = "sha256:" + sha256(canonical(record − {record_hash, signature}))
prev_hash   = previous record's record_hash      (GENESIS for the first)
evidence_id = "OX-" + seq:06d                    (monotonic, never reused, never renumbered)
signature   = "kms-stub:" + record_hash[7:23]    (production: KMS asymmetric signature)
```

Tampering with any field of any record breaks its own hash and every `prev_hash` after it, which is
what V-001 checks before it believes anything else in the bundle.

## 1. Record types (closed set)

| `record_type` | written by | key fields | idempotency key |
|---|---|---|---|
| `API_CALL` | OPS-PROFILE (via the gateway) | `endpoint`, `request_hash`, `response_hash`, `status`, `latency_ms`, `attempt` | (endpoint, canonical request, status) |
| `PROFILE` | OPS-PROFILE | `profile_id`, `customer_id`, `as_of`, `factors`, `coverage_pct`, `profile_hash` | (profile_id) |
| `PROFILE_DELTA` | OPS-PROFILE | `customer_id`, `previous_profile_id`, `changes[]` | (customer, previous, canonical changes) |
| `VERIFICATION` | OPS-VERIFY | `verification_id`, `case_id`, `subject_agent`, `critic_status`, `checks[]`, `blocking[]` | (verification_id, critic_status) |
| `RFI` | OPS-OUTREACH | `event` (`DRAFTED`/`SENT`/`RESPONSE_ASSESSED`/`REFUSED`), `rfi_id`, `case_id`, `round`, `state`, `message_hash`, `approval` | (rfi_id, event, state, round) |
| `DISPOSITION` | OPS-DECIDE | `disposition_id`, `case_id`, `recommendation`, `policy_outcome`, `admissible`, `human_approval_required`, `finalised: false`, `content_hash` | (disposition_id) |
| `EVENT` | any | `event`, `detail` | none (events are meant to repeat) |

An unknown `record_type` fails `make contracts`. The set is closed so that a reader can enumerate what
evidence can exist without reading the code.

## 2. Why hashes and not content

| what | stored | not stored | why |
|---|---|---|---|
| customer master | `response_hash` | the record itself | the profile carries the derived fields; the ledger proves *which* response they came from |
| API request | `request_hash` | the query with PII | a request containing a name is still a name |
| outbound letter | `message_hash` | the text | reproduces the letter from the pinned template; no third-party data at rest in the ledger |
| profile / disposition | `profile_hash` / `content_hash` over the artefact minus its own id | — | the artefact lives in `out/`; the hash binds it to the ledger |

The one place full content is stored is the `VERIFICATION` record's `checks[]` — verdicts, severities
and detail strings. That is deliberate: a critic verdict is only useful if you can read what it
checked, and the detail strings are written to contain no PII (V-011 would fail them if they did).

## 3. The evidence map: which skill writes what

| skill | records | ids cited by |
|---|---|---|
| SK-P1 staged acquisition | `API_CALL` × endpoints, × pages | `data_quality.api.evidence_ids` |
| SK-P2 customer 360 | `API_CALL` (c360, related parties) | `F-CUST-01/06/07`, `F-CHAN-01/02` observations |
| SK-P3 transactions | `API_CALL` (summary + every flow page) | `F-BEH-01/02/03`, `F-PROD-01` |
| SK-P4 counterparties | `API_CALL` per counterparty | `F-PROD-01`, `F-CHAN-01` |
| SK-P5 geography | `API_CALL` (lists) | `F-GEO-02..05` |
| SK-P6 ownership | `API_CALL` (ownership, parties) | `F-CUST-04/05/07` |
| SK-P7 screening | `API_CALL` (lists) + profile record | `screening` block, `F-CUST-03` |
| SK-P8 coverage | `API_CALL` (documents) | profile record, `coverage` block |
| SK-P9 factor mapping | `PROFILE` | `profile.evidence_id`, every `observations[*].evidence_ids` |
| SK-P10 delta | `PROFILE_DELTA` | `delta[].kind` records |
| SK-V1..V5 | `VERIFICATION` | `report.evidence_id`, `disposition.verification.verification_id` |
| SK-O1..O6 | `RFI` × events | `rfi.evidence_id`, `disposition.outreach` |
| SK-D1..D7 | `DISPOSITION`, `EVENT` | `disposition.evidence_id`, `evidence_ids` map |
| every agent's runs | `EVENT` (`RUN_STARTED`, `REDECISION_DIFF`, `BUDGET_EXHAUSTED`) | run summary, operator console |

## 4. Idempotency, and why a re-run is free

Every write passes an idempotency key. The store is a JSON file (`out/state/idem.json`) mapping key →
`evidence_id`; a repeat write returns the existing id and appends nothing.

Two consequences the tests pin down:

* `test_same_input_same_profile_id_and_no_duplicate_evidence` — building the same profile twice leaves
  the ledger length unchanged.
* `test_kill_and_resume_yields_the_same_profile_without_new_evidence` — resuming after a crash replays
  checkpointed bodies, so the resumed run adds no `API_CALL` records for stages it had already done.

The key deliberately excludes the retry `attempt` number: a flaky endpoint that fails once and then
succeeds must not write two successes. Status is part of the key, so a 503 and its later 200 are both
visible.

## 5. Chain verification

`OpsLedger.verify_chain()` walks the file and checks, for each record:

1. `seq` increments by exactly one from 1;
2. `prev_hash` equals the previous record's `record_hash`;
3. `record_hash` re-derives from the canonical record;
4. `signature` matches the `kms-stub:` construction.

It returns `(ok, head, broken_at)`. V-001 runs it on **every** verification and fails the bundle on a
mismatch — a broken chain means the profile's numbers may have been edited, and no downstream
arithmetic can rescue that.

What the chain does **not** prove: that the API told the truth, that the analyst read the document, or
that the policy pack was right. Those are separate controls — V-003 re-derives arithmetic, V-004
replays policy, and the human approval step exists precisely because a valid chain is not the same as
a correct judgement.

## 6. Retention and rotation

| record | retention | notes |
|---|---|---|
| `API_CALL` | 5 years | metadata only; no PII at rest |
| `PROFILE` / `PROFILE_DELTA` | 5 years | hash binds to the artefact in `out/` |
| `VERIFICATION` | 5 years | contains check details, which are the audit trail of the critic |
| `RFI` | 5 years | message hash plus approval record; the letter is reproducible |
| `DISPOSITION` | 7 years | the record a regulator asks for first |
| `EVENT` | 2 years | operational |

Rotation moves a day's records to a WORM bucket with the head hash published to the next day's file
(the first record's `prev_hash` equals yesterday's head). `make clean` archives rather than deletes:
`out/archive/ops_evidence.<epoch>.jsonl`. Nothing in this package ever rewrites a ledger line, and
there is no `evidence.rewrite` capability to grant.
