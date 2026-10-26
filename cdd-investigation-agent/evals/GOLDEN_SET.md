# Synthetic CDD golden set

This corpus supplies a reproducible **synthetic oracle** for development and comparative
evaluation. Every person, company, document, query result, and identifier is fictional.
The oracle is derived from the fixtures and `config/policy.yaml`; it has **not** been
reviewed by a senior KYC analyst. Every `gold.json` records `human_reviewed: false` and
`release_gate_eligible: false`. The release gate described in `spec/07-evaluation.md`
requires human review before these labels can be used for that purpose.

## Coverage

| Segment | Cases | Pages per case | First case |
| --- | ---: | ---: | --- |
| Simple corporate | 40 | 20–60 | `CASE-700001` |
| Complex corporate | 60 | 150–400 | `CASE-700041` |
| Standard individual | 40 | 10–30 | `CASE-700101` |
| Individual PEP or adverse media | 30 | 20–80 | `CASE-700141` |
| High-risk jurisdiction | 30 | 60–150 | `CASE-700171` |
| Adversarial | 40 | 40–120 | `CASE-700201` |
| Degraded input | 20 | 80–180 | `CASE-700241` |
| **Total** | **260** | **29,087 pages** | |

There are 180 legal-entity cases and 80 individual cases. The policy-derived risk bands
are 118 low, 15 medium, 112 high, and 15 very high. Across the set are 2,738 documents,
2,755 quoted evidence anchors, 2,038 fields marked verified, and 402 mock web pages.
Variation includes:

- Direct, layered, trust, and nominee ownership; 45 cases exceed the configured
  look-through depth and 95 have an unresolved nominee share.
- True PEP and sanctions hits, sanctions namesakes, adverse media at allegation and
  final-notice stages, and unavailable screening or registry connectors.
- Stale addresses, contradictory company numbers, missing source-of-funds documents,
  inconsistent activity, and third-party funding.
- Forty distinct document-borne prompt injections. The injected page is quarantined
  and the gold requires ignoring its instructions.
- Five cases each with approximately 5%, 15%, 25%, or 35% unreadable pages. The 35%
  cases exercise the `G1` unusable-bundle gate.

## Files and labels

`evals/golden/manifest.json` lists every case, segment, tags, risk band, and SHA-256
of its gold file. A case directory contains:

```text
CASE-700001/
  case.json                 case input matching schemas/case-input.schema.json
  bundle.json               document inventory
  pages/DOC-700001-001.json page text and extraction confidence
  connector_results.json    seeded offline screening and registry responses
  search_request.json       request for the external-search agent
  search_mock/sites/*.json  case-specific mock web search/fetch corpus
  gold.json                 evaluator-only labels
```

`gold.json` provides source spans (`evidence_anchors`), a narrower set of
`verified_fields`, beneficial ownership paths and effective percentages, screening
outcomes, activity math, risk factor inputs and rating, gaps, required refusals,
human gates, and recommendation. A quoted source assertion is not automatically a
verified fact: contradictory registry claims, stale addresses, customer declarations,
and injected text are kept as evidence anchors without being marked verified.
`search_mock` labels specify relevant media pages, namesakes to exclude, injected
pages to quarantine, and robots or paywall outcomes.

## Use in a benchmark

Give each agent `case.json`, `bundle.json`, `pages/`, `connector_results.json`,
`search_request.json`, `search_mock/`, and
the same pinned policy. Keep `gold.json` and `manifest.json` outside both agent
workspaces. The exporter creates such a workspace and rewrites the case's bundle
reference to the local `bundle.json`:

```bash
.venv/bin/python evals/export_blind_case.py CASE-700001 out/blind/CASE-700001
```

To exercise the sibling `external-search-agent` against a case's mock web corpus:

```bash
.venv/bin/python evals/run_search_mock.py CASE-700141
# writes out/golden-search/CASE-700141/evidence_pack.json
```

The mock pages use the external agent's `Fetcher` format and source policy. They are
generated ahead of time from the same case seed; a search query retrieves them through
the agent's deterministic lexical index. The original 15-page web corpus in
`external-search-agent/corpus/sites/` remains available for its own tests. The
screening and registry connector responses are separate fixtures in
`connector_results.json`.

`compare_corpus.py` runs both implementations against these blind exports. By default
it uses five representative cases; `--all` selects all 260. Pi is opt-in because it
uses a live model provider. Results are written per case under
`out/corpus_benchmark/CASE-*/result.json`, with raw Pi events and stderr beside them,
and an aggregate `out/corpus_benchmark/comparison.json`. The report includes field
matches, schema and exact-quote checks, wall time, provider-reported Pi tokens/cost,
and failure status by segment and risk band. The deterministic corpus reference
uses local compute, while Pi cost is provider-reported and charged by the model
provider.

```bash
.venv/bin/python evals/compare_corpus.py --dry-run
.venv/bin/python evals/compare_corpus.py --pi
.venv/bin/python evals/compare_corpus.py --all --pi --workers 4 --resume
.venv/bin/python evals/compare_corpus.py --segment individual_pep_media --pi --limit 10
```

The corpus runner uses `src/cdd_agent/corpus_reference.py` for an input-driven
reference answer. It parses the blind page sources, computes ownership paths,
triages seeded screening and media, calculates activity ratios and policy factors,
then emits the same compact JSON schema as Pi. It checks case ID, input hashes,
schema, and exact source quotes before scoring. The original `CASE-4831` demo
orchestrator remains in `evals/compare.py`; the corpus reference is a distinct
implementation and does not produce its dossier or hash-chained evidence ledger.
The 260-case reference run reached 260/260 valid answers, 100% field agreement with
the machine-derived synthetic oracle, and 1,583/1,583 exact cited quotes. This is
fixture agreement and is not a production accuracy or human review claim.

Pi receives the same case bundle, seeded connector responses, mock search pages,
and pinned policy in a temporary workspace. The gold file and manifest are retained
by the evaluator. The compact JSON output is an evaluation projection; it does not
test dossier, evidence ledger, resumability, or human review execution. The mock
search corpus is generated with the cases, while `run_search_mock.py` can separately
exercise the sibling search agent against it.

For a small read-through, start with `CASE-700001` (simple corporate), `CASE-700041`
(medium-risk layered ownership), `CASE-700141` (individual media/screening),
`CASE-700201` (injection and contradictory registry number), and `CASE-700241`
(35% unreadable pages and screening outage).

## Reproduce and validate

```bash
.venv/bin/python evals/validate_golden.py
.venv/bin/python evals/generate_golden.py --force
.venv/bin/python evals/validate_golden.py
```

The generator uses fixed seed `20261022` and deterministic fixture timestamps. The
validator checks schemas, counts and page ranges, document/page references, exact
quote spans, ownership arithmetic and UBO threshold, screening outcomes, activity
arithmetic, risk factors against source facts and the pinned policy, gates, and the
manifest hashes. A full integration run through the sibling external-search agent
also confirmed all 260 cases' media, injection, robots, and paywall labels.
Regeneration replaces `evals/golden/`; validation is read-only.

These are JSON text fixtures. The encrypted, rotated, mixed-language, oversized,
and illegible cases model extraction failure through metadata and unreadable-page
markers; they do not contain actual PDF or image rendering problems. Long bundles
include many unique supporting-record pages, so page count and navigation load are
represented, but the density of distinct business facts is lower than in a real
400-page KYC file. Add analyst-reviewed and real-format cases before using results
to make a production readiness decision.
