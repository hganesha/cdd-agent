PY ?= python3

.PHONY: help run verify kill resume budget test corpus-tests contracts benchmark benchmark-pi benchmark-corpus benchmark-corpus-pi golden-check golden-regenerate search-mock demo clean all

help:
	@echo "ATLAS-CDD — CDD investigation agent"
	@echo ""
	@echo "  make run        full investigation on the demo case"
	@echo "  make verify     verify the evidence hash chain"
	@echo "  make kill       run and simulate a crash after 8 work units"
	@echo "  make resume     resume from the last checkpoint"
	@echo "  make budget     run with a \$6 budget to force a PARTIAL delivery"
	@echo "  make test       resumability / guardrail tests (9)"
	@echo "  make corpus-tests check blind export and fail-closed comparison scoring"
	@echo "  make contracts  validate every artifact against its JSON Schema"
	@echo "  make benchmark compare the reference on the synthetic case"
	@echo "  make benchmark-pi compare reference and Pi on the same case"
	@echo "  make benchmark-corpus run the reference on five golden cases"
	@echo "  make benchmark-corpus-pi compare reference and Pi on five golden cases"
	@echo "  make golden-check validate the 260-case synthetic oracle"
	@echo "  make golden-regenerate rebuild the oracle from the fixed seed"
	@echo "  make search-mock CASE=CASE-700141 run the search mock for one case"
	@echo "  make demo       kill -> resume -> verify, then show the manifest"
	@echo "  make clean      remove run artefacts"

run:
	cd src && $(PY) -m cdd_agent.cli run --clean

verify:
	cd src && $(PY) -m cdd_agent.cli verify

kill:
	cd src && $(PY) -m cdd_agent.cli run --clean --kill-at 8 || true

resume:
	cd src && $(PY) -m cdd_agent.cli resume

budget:
	cd src && $(PY) -m cdd_agent.cli run --clean --budget 6

test:
	$(PY) tests/test_resumability.py

corpus-tests:
	$(PY) tests/test_compare_corpus.py

contracts:
	$(PY) tests/test_contracts.py

benchmark:
	$(PY) evals/compare.py

benchmark-pi:
	$(PY) evals/compare.py --pi

benchmark-corpus:
	$(PY) evals/compare_corpus.py

benchmark-corpus-pi:
	$(PY) evals/compare_corpus.py --pi

golden-check:
	$(PY) evals/validate_golden.py

golden-regenerate:
	$(PY) evals/generate_golden.py --force
	$(PY) evals/validate_golden.py

search-mock:
	$(PY) evals/run_search_mock.py $(CASE)

demo: kill resume verify
	@echo ""
	@echo "── run manifest ──"
	@$(PY) -c "import json;m=json.load(open('out/run_manifest.json'));\
	print(json.dumps({k:m[k] for k in ('status','coverage','risk','critic','required_gates','counts')},indent=1))"

clean:
	cd src && $(PY) -m cdd_agent.cli clean
	rm -rf out/tests out/contracts

all: test contracts
