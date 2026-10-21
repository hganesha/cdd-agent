PY ?= python3

.PHONY: help run verify kill resume budget test contracts demo clean all

help:
	@echo "ATLAS-CDD — CDD investigation agent"
	@echo ""
	@echo "  make run        full investigation on the demo case"
	@echo "  make verify     verify the evidence hash chain"
	@echo "  make kill       run and simulate a crash after 8 work units"
	@echo "  make resume     resume from the last checkpoint"
	@echo "  make budget     run with a \$6 budget to force a PARTIAL delivery"
	@echo "  make test       resumability / guardrail tests (9)"
	@echo "  make contracts  validate every artifact against its JSON Schema"
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

contracts:
	$(PY) tests/test_contracts.py

demo: kill resume verify
	@echo ""
	@echo "── run manifest ──"
	@$(PY) -c "import json;m=json.load(open('out/run_manifest.json'));\
	print(json.dumps({k:m[k] for k in ('status','coverage','risk','critic','required_gates','counts')},indent=1))"

clean:
	cd src && $(PY) -m cdd_agent.cli clean
	rm -rf out/tests out/contracts

all: test contracts
