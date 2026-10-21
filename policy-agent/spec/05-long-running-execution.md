# 05 · Long-running execution, latency and durability

The policy agent is the *short* call in a long workflow: a decision takes single-digit milliseconds.
The long-running problem it owns is different, and it is the one that actually bites in production —
**changing policy while cases are in flight**, and **re-deciding thousands of cases under a new
revision** without losing or duplicating any of them.

## 1. Latency envelope (measured, `opa eval` against the local bundle)

| Operation | Observed | Budget | Note |
|---|---|---|---|
| One `decide` (full envelope) | ≈ 4 ms | 150 ms p99 | includes process start of the OPA CLI |
| `authority` (single action) | ≈ 3 ms | 50 ms | table lookup + default-deny |
| `opa test policy/` | ≈ 0.6 s | 60 s in CI | 35 tests |
| `bundle build` + `verify` | ≈ 0.4 s | 5 s | 11 files, ~10 KB archive |
| `corpus` (10 cases) | ≈ 0.15 s | 5 s | the regression baseline |

In a deployment, `OPAClient` should hold one long-lived OPA server (`opa run --server`) and POST to
`/v1/data/agent/decision/result`; the CLI path used here exists so the whole package is runnable and
testable without infrastructure. The policy itself is unchanged either way — that is the point of
putting the logic in Rego rather than in Python.

## 2. Re-deciding a portfolio (the real long-running job)

A threshold change (e.g. UBO 25 % → 20 %) affects every open case. The agent's job is to compute the
effect **before** anyone approves it, and to be able to re-decide afterwards without double-counting.

```
   ┌────────────────────────────────────────────────────────────────┐
   │ 1. candidate change → sandbox copy of policy/                  │
   │ 2. opa check (restricted capabilities) + opa test (full suite)  │
   │ 3. impact: evaluate all N cases under the candidate             │
   │    → per-case diff: outcome, reasons, obligations, route        │
   │    → materiality: LOW / MEDIUM / HIGH from the blast radius     │
   │ 4. DRAFT proposal (never ACTIVE)                                │
   └────────────────────────────────────────────────────────────────┘
                                  │ human approval (named + ticket)
                                  ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ 5. build bundle → verify hashes → activate revision             │
   │ 6. re-decide queue: for each case, evaluate under the new       │
   │    revision; ledger key (decision_id, sha256(input)) means a    │
   │    case re-decided under a *new* policy appends a new record    │
   │    while a retry under the *same* policy appends nothing        │
   └────────────────────────────────────────────────────────────────┘
```

Three properties make this safe:

* **Idempotent per (case, policy).** A crashed re-decide loop can be restarted from the beginning;
  cases already decided under the current revision produce no new records.
* **Deterministic per (case, policy).** The same case under the same revision cannot produce two
  different outcomes, so a "changed" record always means a real policy change.
* **Bounded work per case.** `decide` is a pure function of the input and the bundle; there is no
  state, no cache to warm, no ordering dependency. Re-deciding 50 000 cases is a loop, not a project.

Current implementation evaluates the corpus (10 cases). Scale-out is a queue over case files with the
same `axiom.cli replay`-style comparison; the policy, the keys and the ledger do not change.

## 3. Chunking and checkpointing for `author` / `replay` at scale

`axiom author` runs a corpus-wide impact analysis; at portfolio scale the same routine takes a
`--shard i/n` and writes one result file per shard, aggregated afterwards. The rule holds: **every
shard is independently re-runnable and independent of the others**, because a case's outcome depends
only on its own input and the candidate policy.

State that must survive a restart:

| Artefact | Why |
|---|---|
| `proposals/candidates/<id>.rego` | the exact candidate that was measured; an impact report without its candidate is unreproducible |
| `proposals/<id>.json` | validation results, impact diffs, materiality, DRAFT status |
| `out/bundles/<rev>.{tar.gz,manifest.json}` | the revision every subsequent record names |
| `out/state/idem.json` | idempotency index for the ledger |

## 4. Failure modes and their handling

| Failure | Effect | Handling |
|---|---|---|
| Bundle hash mismatch on activation | activation refused | `bundle.activate` verifies every file hash and the archive digest first; an incident record is written |
| Policy compiles but a test fails | candidate cannot be staged | `author` exits 1, DRAFT written with `validation.compiled: true, suite: "34/35"` and the failing test named |
| Restricted capability violated (`http.send`) | `opa check` fails | `test_policy_compiles_under_restricted_capabilities` proves the check is live by injecting the builtin |
| Undefined result (missing `default`) | decision is `null` | `cli decide` exits 2 with "policy returned no decision" — never a silent `PERMIT` |
| Ledger write fails | decision not delivered | write precedes the response; a decision without a record is not returned |
| OPA binary absent/old | wrapper refuses to run | `OPAClient` asserts version ≥ 1.21 and the vendored `bin/opa` is the default |

The one that deserves emphasis is **undefined ≠ permit**. Every rule that could be vacuously true has
an explicit `default`, and `cli decide` treats a `null` result as an error. A policy mistake must fail
closed.

## 5. Operating rhythm

| Activity | Cadence | Owner | Evidence |
|---|---|---|---|
| `opa test policy/` | every commit | CI | `TEST_RUN` record |
| corpus regression (10 cases) | every commit | CI | `TEST_RUN` record |
| bundle build + verify | every merge to `policy/` | CI | `BUNDLE/BUILT` + `BUNDLE/VERIFIED` |
| bundle activation | on approval only | Platform Security | `BUNDLE/ACTIVATED` with `approver` + `ticket` |
| clause → proposal sweep | monthly, or on SOP revision | Policy team | `PROPOSAL` records, including rejected ones |
| re-decide open portfolio | after every activation | Case ops | per-case `DECISION` records |

## 6. What is *not* long-running here, on purpose

No model is in the evaluation path, so there is no token budget, no context limit, no retry ladder
with drifting temperature, no partial answer. The long-running complexity lives entirely in the two
places where it is honest: the **portfolio sweep** (§2) and the **policy authoring loop** (§3), both of
which are loops over a deterministic function — the easiest kind of long-running work to make correct.
