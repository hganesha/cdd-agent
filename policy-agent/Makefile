# AXIOM-POLICY — deterministic CDD decision service (OPA/Rego)
SHELL := /bin/bash
PY    ?= python3
OPA   ?= bin/opa
INPUT ?= /tmp/decisive.json

.PHONY: help check test rego corpus contracts decide disposition replay authority ledger \
        build verify activate author check-prompt check-authority fmt clean

help:
	@echo "AXIOM-POLICY (cdd-policy-2026.4 · OPA $(shell $(OPA) version 2>/dev/null | sed -n 's/^Version: //p'))"
	@echo "  make check          opa test + python suite + corpus + contracts"
	@echo "  make test           python end-to-end properties (22)"
	@echo "  make rego           opa test policy/ (44)"
	@echo "  make corpus         10-case regression against the pinned baseline"
	@echo "  make decide         evaluate \$$INPUT and print the envelope"
	@echo "  make disposition    band x recommendation x gates x critic_status -> ADMIT/REJECT"
	@echo "  make replay         re-derive a stored decision from its input"
	@echo "  make authority      action authority (default: case.approve)"
	@echo "  make ledger         verify the decision evidence chain"
	@echo "  make build          build + verify + stage a bundle (REV=r-002)"
	@echo "  make author         turn an SOP clause into a DRAFT proposal (PROP=P-2026-013)"
	@echo "  make contracts      schema / pin / corpus / ledger / authority-drift checks"
	@echo "  make fmt            opa fmt -w policy/"
	@echo "  make clean          remove generated output (ledger and bundles kept)"

rego:
	$(OPA) test policy/

test:
	$(PY) tests/test_policy_agent.py

corpus:
	cd src && $(PY) -m axiom.cli corpus

check: rego test corpus contracts
	@echo
	@echo "AXIOM-POLICY: all checks passed"

contracts:
	$(PY) scripts/check_contracts.py

decide:
	cd src && $(PY) -m axiom.cli decide --input $(INPUT)

# The disposition contract the four OPS agents call: is this recommendation admissible for this
# band, and may any agent finalise it? Input shape:
#   {"band":"HIGH","recommendation":"ESCALATE_EDD","required_gates":["OBTAIN_SOURCE_OF_WEALTH"],
#    "critic_status":"PASS"}
disposition:
	@test -f $(or $(DISP),/tmp/disposition.json) || printf '%s\n' \
	  '{"band":"HIGH","recommendation":"ESCALATE_EDD","required_gates":[],"critic_status":"PASS"}' \
	  > $(or $(DISP),/tmp/disposition.json)
	$(OPA) eval -d policy/ --input $(or $(DISP),/tmp/disposition.json) \
	  'data.agent.disposition.result' --format pretty

replay:
	cd src && $(PY) -m axiom.cli replay --input $(INPUT)

authority:
	cd src && $(PY) -m axiom.cli authority --action $(or $(ACTION),case.approve)

ledger:
	cd src && $(PY) -m axiom.cli ledger

build:
	cd src && $(PY) -m axiom.cli build --revision $(or $(REV),r-002) --stage

verify:
	cd src && $(PY) -m axiom.cli verify --revision $(or $(REV),r-002)

activate:
	@test -n "$(APPROVER)" || (echo "APPROVER and TICKET are required: make activate REV=r-002 APPROVER=HEAD_OF_FCC TICKET=CAB-118"; exit 1)
	cd src && $(PY) -m axiom.cli activate --revision $(or $(REV),r-002) --approver "$(APPROVER)" --ticket "$(TICKET)"

author:
	@test -n "$(CHANGE)" || (echo "CHANGE and DOC are required: make author PROP=P-2026-014 CHANGE=/tmp/change.json DOC=corpus/SOP_CDD_v4.2_excerpt.md"; exit 1)
	cd src && $(PY) -m axiom.cli author --proposal $(or $(PROP),P-2026-014) \
	  --change $(CHANGE) --doc $(or $(DOC),corpus/SOP_CDD_v4.2_excerpt.md)

check-prompt:
	@$(PY) -c "import hashlib,json,pathlib,sys;\
pins=json.loads(pathlib.Path('prompts/pins.json').read_text())['files'];\
bad=[f for f,d in pins.items() if 'sha256:'+hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest()!=d];\
print('  ok    prompts pinned' if not bad else '  FAIL  unpinned change: '+', '.join(bad));\
sys.exit(1 if bad else 0)"

check-authority:
	@$(PY) scripts/check_authority.py >/dev/null && echo "  ok    authority projection agrees with the Rego"

fmt:
	$(OPA) fmt -w policy/

clean:
	rm -rf out/last_decision.json out/state/idem.json src/axiom/__pycache__ tests/__pycache__
	@echo "cleaned (ledger, bundles and proposals kept)"
