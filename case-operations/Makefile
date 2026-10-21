# OPS case operations — four agents, one runtime, one ledger, one policy service
SHELL := /bin/bash
PY    ?= python3
CASE  ?= CASE-4831
SRC   := src

.PHONY: help run profile verify rfi decide ledger kill resume budget test contracts demo clean all

help:
	@echo "OPS case operations — OPS-PROFILE · OPS-VERIFY · OPS-OUTREACH · OPS-DECIDE"
	@echo ""
	@echo "  make run        full loop on \$$CASE (default CASE-4831): profile -> verify -> RFI -> decide"
	@echo "  make profile    build the profile only"
	@echo "  make verify     re-verify the stored profile"
	@echo "  make rfi        draft the customer request (never sends)"
	@echo "  make decide     disposition, composed with the policy service"
	@echo "  make ledger     verify the evidence hash chain"
	@echo "  make kill       run and crash at API call 5 (exit 70)"
	@echo "  make resume     resume from the checkpoint"
	@echo "  make budget     run with a 4-call API budget to force a PARTIAL profile"
	@echo "  make test       the 53-agent acceptance suite"
	@echo "  make contracts  schemas, prompts pins, ledger, policy service"
	@echo "  make demo       both corpus cases, kill/resume, ledger, contracts"
	@echo "  make clean      remove run artefacts (the ledger is archived, not deleted)"
	@echo ""
	@echo "  CASE=CASE-4902 make run     the clean case"

run:
	cd $(SRC) && $(PY) -m opsagents.cli run --case $(CASE)

profile:
	cd $(SRC) && $(PY) -m opsagents.cli profile --case $(CASE)

verify:
	cd $(SRC) && $(PY) -m opsagents.cli verify --case $(CASE)

rfi:
	cd $(SRC) && $(PY) -m opsagents.cli rfi --case $(CASE)

decide:
	cd $(SRC) && $(PY) -m opsagents.cli decide --case $(CASE)

ledger:
	cd $(SRC) && $(PY) -m opsagents.cli ledger

kill:
	cd $(SRC) && $(PY) -m opsagents.cli profile --case $(CASE) --fresh --kill-at 5 || true

resume:
	cd $(SRC) && $(PY) -m opsagents.cli profile --case $(CASE) --resume

budget:
	cd $(SRC) && $(PY) -m opsagents.cli profile --case $(CASE) --fresh --budget 4 || true

test:
	$(PY) tests/test_ops_agents.py

contracts:
	$(PY) scripts/check_contracts.py

demo: clean
	@set -e; \
	  $(MAKE) run CASE=CASE-4831; \
	  $(MAKE) run CASE=CASE-4902; \
	  echo "--- crash and resume ---"; \
	  $(MAKE) kill CASE=CASE-4831; \
	  $(MAKE) resume CASE=CASE-4831; \
	  echo "--- ledger and contracts ---"; \
	  $(MAKE) ledger; \
	  $(MAKE) contracts
	@echo ""
	@echo "demo complete"

all: test contracts
	@echo ""
	@echo "OPS case operations: tests and contracts passed"

clean:
	@mkdir -p out/archive
	@if [ -f out/ops_evidence.jsonl ]; then \
	  mv out/ops_evidence.jsonl out/archive/ops_evidence.$$(date +%s).jsonl; echo "archived the ledger"; fi
	@rm -rf out/state out/profiles out/dispositions out/rfi
	@echo "cleaned (archive kept)"
