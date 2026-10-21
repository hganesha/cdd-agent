# WEBIS-SEARCH — external search agent
SHELL := /bin/bash
PY    ?= python3
SUBJECT ?= Meridian Trade Holdings Ltd
PARTY   ?= Elena Vasquez

.PHONY: help check test demo verify ledger contracts check-prompt clean baseline diff-pack

help:
	@echo "WEBIS-SEARCH"
	@echo "  make check        contracts + 54 acceptance tests + quotation integrity"
	@echo "  make test         54 acceptance tests only"
	@echo "  make demo         the reference run, printed"
	@echo "  make verify       re-derive every quotation from the corpus"
	@echo "  make ledger       verify the evidence chain"
	@echo "  make contracts    schema / prompt-pin / ledger checks on existing artefacts"
	@echo "  make baseline     snapshot the current pack as the regression baseline"
	@echo "  make diff-pack    diff the current pack against the baseline (claims only)"
	@echo "  make resume       interrupt a run after 3 queries, then resume it"
	@echo "  make clean        remove generated artefacts (keeps the corpus and the ledger)"

test:
	$(PY) tests/test_search_agent.py

contracts:
	$(PY) scripts/check_contracts.py

check:
	@$(PY) -m compileall -q src/webis >/dev/null && echo "  ok    sources compile"
	@$(MAKE) --no-print-directory demo >/dev/null
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory contracts
	@echo
	@echo "WEBIS-SEARCH: all checks passed"

demo:
	cd src && $(PY) -m webis.cli search --subject "$(SUBJECT)" \
		--related-party "$(PARTY)" --dob 1979-03-14 --country VE --max-queries 18

verify:
	cd src && $(PY) -m webis.cli verify

ledger:
	cd src && $(PY) -m webis.cli ledger

resume:
	cd src && $(PY) -m webis.cli search --subject "$(SUBJECT)" --max-queries 18 --stop-after 3 ; \
	cd src && $(PY) -m webis.cli search --subject "$(SUBJECT)" --max-queries 18 --resume

baseline:
	mkdir -p out/baseline
	$(PY) -c "import json,pathlib;p=json.loads(pathlib.Path('out/evidence_pack.json').read_text());\
pathlib.Path('out/baseline/evidence_pack.json').write_text(json.dumps(p,indent=2)+'\n');\
print('baseline: %d claim(s)' % len(p['claims']))"

diff-pack:
	$(PY) -c "import json,pathlib;\
a=json.loads(pathlib.Path('out/evidence_pack.json').read_text())['claims'];\
b=json.loads(pathlib.Path('out/baseline/evidence_pack.json').read_text())['claims'];\
ka={c['claim_key']:c['confidence'] for c in a};kb={c['claim_key']:c['confidence'] for c in b};\
d=[f'{k}: {kb.get(k)} -> {ka.get(k)}' for k in sorted(set(ka)|set(kb)) if ka.get(k)!=kb.get(k)];\
print('\n'.join(d) if d else 'no confidence changes against the baseline')"

clean:
	rm -rf out/cache out/state out/evidence_pack.json out/partial_result.json
	@echo "cleaned (ledger and baseline kept)"
