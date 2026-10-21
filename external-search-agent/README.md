# WEBIS-SEARCH

A governed external search agent for KYC. Give it a subject and the attributes that distinguish them
from their namesakes; it returns an **evidence pack**: atomic claims, each carried only by quotations
that exist at a recorded offset in a document whose hash was recorded at retrieval, each weighted by
source quality and by how many genuinely *independent* origins assert it — plus an explicit account of
everything it refused, quarantined or could not reach.

It does not decide anything. It does not rate risk, clear a subject, or write to a KYC file. CDD
investigation forms findings; the OPA policy agent makes decisions. This agent produces evidence that
both can cite.

```
  subject + attributes
        │
        ├─ SK-S1  plan queries (facets, related parties)             ─► QUERY records
        ├─ SK-S2  source policy: tier, deny-list, citability          ─► refusals
        ├─ SK-S3  retrieve: robots, paywalls, licences, rate limits   ─► RETRIEVAL records
        ├─ SK-S9  quarantine pages that instruct the agent            ─► QUARANTINE records
        ├─ SK-S4  extract passages with exact character spans
        ├─ SK-S5b exclude same-name passages (dob / age / location)
        ├─ SK-S5  credibility, factor by factor
        ├─ SK-S6  triangulate by ORIGIN, not by domain
        ├─ SK-S7  citation pack + confidence                          ─► CLAIM / PACK records
        ├─ SK-S10 redact PII before disclosure
        └─ SK-S11 checkpoint after every query; resume on demand
```

## Quickstart

```bash
cd external-search-agent

make check          # contracts + 54 acceptance tests + quotation integrity
make demo           # the reference run, printed
make verify         # re-derive every quotation from the corpus  → 100.0 % precision
make ledger         # verify the append-only evidence chain      → chain_valid: true
```

Raw CLI (no make):

```bash
cd src
python3 -m webis.cli search --subject "Meridian Trade Holdings Ltd" \
        --related-party "Elena Vasquez" --dob 1979-03-14 --country VE --max-queries 18
python3 -m webis.cli verify
python3 -m webis.cli ledger
```

Requirements: Python 3.11+, `pyyaml`, `jsonschema`. No network access is needed — the corpus in
`corpus/sites/` is a deterministic, hostile miniature of the web (see §The corpus).

## What a run looks like

```
=== SR-0001 · Meridian Trade Holdings Ltd · PARTIAL ===

  [HIGH  ] A regulator has taken public enforcement action naming the subject.
            corroboration: 2 independent origin(s) across 2 domain(s)
            - https://fca.org.uk/news/final-notices/2024/meridian-trade-holdings
              (tier 1, credibility 80.0) EV-abfd82c9ae98
              "The Financial Conduct Authority has published a final notice in respect of …"
              (+2 more passage(s) from this document)
            - https://financialgazette.example/2026/02/03/meridian-administration
              (tier 2, credibility 40.0) EV-7db372a41898

  [MEDIUM] The regulator characterised the conduct as negligent, not dishonest.
            corroboration: 1 independent origin(s) across 1 domain(s)
            ~ refresh due: newest source is 714 days old, limit 90 day(s) for this claim family

  [LOW   ] The subject has entered administration.
            corroboration: 1 independent origin(s) across 1 domain(s)
            ! commoditytoday.example attributes its report to 'Global Wire', which was not
              retrieved — corroboration may be an undercount

  QUARANTINED
    - citydesk.example/…: instruction_override, role_reassignment, citation_forcing,
      omission_instruction, concealment_instruction, fake_system_block

  REFUSED
    - viralbiz.example/…: BLOCKED_DOMAIN   (deny-list: adversarial content)
    - scrapedleaks.example/…: BLOCKED_DOMAIN (obtained without consent; personal data)
    - biznewsaggregator.example/…: OK (tier_4_not_citable)
    - financialgazette.example/2026/…: EXCLUDED_SAME_NAME (conflicting date of birth)
    - localnews.example/…: EXCLUDED_SAME_NAME (conflicting date of birth)
    - regionalbusinesswire.example/…: EXCLUDED_SAME_NAME (conflicting location, age)

  CIRCULATING — recorded, not relied upon
    - biznewsaggregator.example/meridian-fraud-claims
      relevant but not citable; the fact that this material is circulating is recorded,
      its content is not relied upon

  cost: {'fetches': 10, 'domains': 9, 'queries': 16}
```

Read the negative half. `PARTIAL` is not a failure — it says a page tried to instruct the agent and
was thrown away. The administration claim is `LOW` **and** carries a syndication note, which is the
whole point of the design: two outlets, one wire, one origin.

## The corpus

15 fixtures across 13 domains, each one a reproducible failure mode: a tier-1 final notice, a
syndicated wire story, a paywall, a robots-disallowed dump, a **prompt-injection page on an allowed
tier-3 domain**, a tier-4 aggregator allegation, three same-name traps (date of birth, age, location),
an on-topic decoy that mentions nobody. Full table with rationale in `spec/00-agent-definition.md §7`.

## Layout

```
spec/00-agent-definition.md    what the agent is; division of labour; design positions
spec/01-system-prompt.md       the prompt's place in the control stack, clause by clause
spec/02-skills.md              12 skills × inputs / tools / outputs / evidence / failure / tests
spec/03-io-contracts.md        6 JSON Schemas and the guarantees they enforce
spec/04-evidence-writes.md     what is written, when, keyed how, and how tampering is detected
spec/05-long-running-execution.md  checkpointing, resume guarantees, budgets, interruption taxonomy
spec/06-guardrails.md          untrusted content, source quality, PII, compliance, limits
spec/07-evaluation.md          metrics, ground truth, what this suite cannot tell you

prompts/system.md              the system prompt (hash-pinned)
prompts/subsystem-prompts.md   SYS-PLAN · SYS-CLAIM · SYS-NOTE · SYS-DESCRIBE
prompts/pins.json              digests; `make check` fails on an unpinned edit

config/sources.yaml            tiers 1–5, deny-list, compliance constants, freshness, PII
config/budget.yaml             query / fetch / domain / wall-clock / spend ceilings

src/webis/fetch.py             FetchResult · SourcePolicy · Fetcher
src/webis/agent.py             planning, extraction, credibility, triangulation, the agent
src/webis/evidence.py          hash-chained ledger, idempotency store
src/webis/cli.py               search · verify · ledger
corpus/sites/                  15 fixture pages across 13 domains

schemas/                       6 JSON Schema 2020-12 contracts
tests/test_search_agent.py     54 tests, one class per skill + contracts
scripts/check_contracts.py     the non-unit checks behind `make check`
out/evidence_pack.json         the hand-off artefact
out/search_evidence.jsonl      the append-only ledger (WS-nnnnnn)
out/state/                     checkpoints and the idempotency store
```

## The five properties worth knowing

1. **Citation or nothing.** `citations` has `minItems: 1` in the schema. A claim without a quotation
   is not representable — a schema wall, not a prompt suggestion.
2. **Independence is by origin.** `"according to a report by Global Wire"` resolves to
   `globalwire.example` even when that page was never fetched. Two outlets that reprint one wire are
   one source, with a note saying so.
3. **Refusal is an output.** Denied, robots-disallowed, paywalled, tier-4 and same-name sources each
   appear with a reason. An agent that hides what it dropped looks exactly like an agent that found
   nothing.
4. **Untrusted content is inert.** Injection scanning runs before extraction; a matching page is
   quarantined whole and contributes zero passages. Tested against an *allowed* domain, so the
   deny-list cannot be what saves the run.
5. **Resumable, for real.** Interrupt with `--stop-after 3`, resume with `--resume`, and the result is
   the same claims as an uninterrupted run — asserted by
   `TestSKS11Resumption.test_interrupted_run_checkpoints_and_resume_reaches_the_same_answer`.

## Long runs

```bash
python3 -m webis.cli search --subject "…" --stop-after 6 ; echo $?   # 3 → interrupted, checkpointed
python3 -m webis.cli search --subject "…" --resume    ; echo $?      # 0 → pack written
```

A checkpoint is written after every completed query (atomic `.tmp` + rename) to
`out/state/run_<request_id>.json`. A resumed run inherits the spend, retries only the interrupted
query, and never re-fetches a page it already paid for. Budget stops keep their checkpoint, so raising
the budget is one flag away. Details: `spec/05-long-running-execution.md`.

## Tests

```
TestSKS1QueryPlanning 4   TestSKS5bSameName 5   TestSKS9Quarantine 3
TestSKS2SourcePolicy 4    TestSKS6Triangulation 3  TestSKS10PII 3
TestSKS3Compliance   5    TestSKS7CitationPack 6   TestSKS11Resumption 4
TestSKS4PassageExtraction 3  TestSKS5Credibility 6  TestSKS8Ledger 3   TestContracts 5
```

Every class builds its own corpus in a temporary directory: editing `corpus/sites/` can never make a
test pass by accident. `make check` runs the tests, then re-derives every quotation, validates every
artefact against its schema, and verifies the ledger chain and the prompt pins.

## Limits, stated plainly

Synthetic corpus — live precision/recall is unknown and needs a shadow-mode pilot with human-labelled
packs. Six injection pattern families, not six hundred. Same-name rules only distinguish on
dob/age/location. Credibility weights are reviewable policy choices, not measurements. The ledger
signature is a KMS stub: the chain is intact, third-party non-repudiation is not yet real. See
`spec/07-evaluation.md §6`.

## Siblings

* `../cdd-investigation-agent/` — consumes packs as findings; 100s-of-pages investigation with
  coverage matrices and gates.
* `../policy-agent/` — the OPA service that decides what may be done with a finding. It never reads a
  pack; the CDD agent hands it facts and it hands back a decision with an evidence record.
