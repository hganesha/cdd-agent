# CASE-4831 reference versus Pi pilot

This is a historical October 22 pilot snapshot. The current media stage locates
printouts from the bundle and records exact quote spans; the generated 260-case
oracle is now available. See `GOLDEN_SET.md` for the current corpus setup and
the remaining reference compatibility limits.

Measured on October 22, 2026 (America/Chicago). The input is one synthetic case:
23 documents and 300 pages. The parent repository describes a larger stack with
separate external-search and policy services. This comparison isolates the CDD
investigator and gives Pi the same case, policy file, and seeded screening responses
used by the CDD reference implementation.

## Reference implementation as is

- Existing tests: 9/9 resumability and guardrail tests passed; all emitted artifacts
  passed their JSON Schema checks, including 119 evidence records.
- One full investigation took about 0.05–0.07 seconds in repeated local runs. It read
  297 pages, marked 3 illegible, produced 12 findings and 119 evidence records, and
  returned HIGH / EDD with gates G2 and G3. The evidence chain verified with zero
  duplicate records.
- All 37 extraction quotations existed verbatim on their recorded pages. Only 35/37
  `char_span` values selected the exact quotation. The two incorrect spans are the
  adverse-media event and namesake exclusion in `sk08_media`; the quotes themselves
  exist on those pages. This is a concrete citation-anchor defect in the baseline.
- The reported $22.96 and token count in the reference manifest are **simulated
  workload estimates**. Its subsecond runtime is an offline deterministic program,
  not a live model-backed investigation.

## Pi implementation and observed performance

`pi_cdd/AGENTS.md` turns Pi into a CDD investigator. `evals/compare.py` creates an
isolated input workspace, invokes Pi with tools, captures provider usage and tool
events, parses its JSON, verifies quotes against source pages, checks a small set of
policy constraints, and compares 14 seeded case assertions. `--repair` asks Pi once
to correct validation errors and records both attempts. A failed validation blocks
delivery; it does not silently turn into a passing result.

Three repeated runs of the first Pi prompt used Pi 0.82.1 and
`openrouter/openai/gpt-4.1-mini` with thinking off:

| Run | Elapsed | Provider cost | Seeded checks | Exact quotes |
|---|---:|---:|---:|---:|
| 1 | 16.4 s | $0.0160 | 8/14 | 9/9 |
| 2 | 12.4 s | $0.0106 | 7/14 | 1/9 |
| 3 | 14.9 s | $0.0161 | 8/14 | 5/8 |

The fixed-prompt runs vary substantially. Pi consistently found the PEP, unresolved
nominee and adverse media, but it inconsistently applied the activity test, ownership
threshold and gates. Invalid quotes included ellipses and changed spacing. Later prompt
and input changes improved individual checks but did not yield a consistently valid
CDD answer. A run with `gpt-5-mini` and low thinking took 45.7 seconds, cost $0.0186,
matched 10/14 seeded assertions, and produced 11/11 exact quotations; it omitted
the PEP match and G2. The latest `gpt-4.1-mini` configuration took 23.9 seconds,
cost $0.0347, matched 5/14 assertions, and was blocked by validation despite 7/7
exact quotations. One repair run remained blocked after a cumulative 22.8 seconds
and $0.0223.

The Pi output is a compact investigation JSON, not the reference's full dossier,
hash-chained evidence ledger, checkpoint/resume state, or schema-conformant CDD memo.
Its seeded-check score is agreement with the synthetic case expectations, not a
senior-analyst accuracy estimate. The recommendation check specifically measures
agreement with the reference's `HOLD_FOR_RFI`; `ESCALATE_EDD` is also permitted by
the HIGH-band policy matrix, so a mismatch there is not by itself a policy breach.
Quote exactness checks literal source text; it does
not prove that the quotation entails the claim. The reference's simulated cost and
Pi's provider-billed cost are different measures and should not be compared as prices.

## Conclusion

The reference is faster and more contract-complete on this one case, with a two-record
span defect. Pi can investigate the case, but free-form CDD reasoning under `AGENTS.md`
is currently too variable for delivery. The parent architecture's deterministic
policy service and governed evidence writes would need to remain in the execution
path for a production Pi adapter. The proposed 260-case gold set is not present here,
so neither variant has a production release score.

## Reproduce

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyyaml jsonschema
.venv/bin/python tests/test_resumability.py
.venv/bin/python tests/test_contracts.py
.venv/bin/python evals/compare.py --pi --out out/benchmark/new-run
# optional validation repair attempt:
.venv/bin/python evals/compare.py --pi --repair --out out/benchmark/new-repair
```

Machine-readable comparisons and Pi event streams live under `out/benchmark/`.
