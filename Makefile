# Charity Care Commons (Texas edition). Everything here runs offline at zero cost
# (replay fixtures) unless ANTHROPIC_API_KEY is set; see README.md.
PYTHON ?= python
PORT ?= 8000
SUITES ?= smoke full test engine packet

.PHONY: help install test lint demo serve site eval gate schema clean

help:  ## list targets
	@grep -E "^[a-z]+:.*##" $(MAKEFILE_LIST) | sed "s/:.*## /\t/"

install:  ## editable install with dev tools
	$(PYTHON) -m pip install -e ".[dev]"

test:  ## full pytest suite (offline, no API key)
	$(PYTHON) -m pytest -q

lint:  ## ruff
	ruff check src evals tests

demo:  ## whole pipeline on the synthetic corpus -> data/demo-run/, site/dist/
	ccc demo

serve:  ## API + review console + navigator on the registry built by `make demo`
	@test -f data/demo-run/registry.sqlite || ccc demo --no-evals
	ccc serve --db data/demo-run/registry.sqlite --port $(PORT) --console-open

site:  ## rebuild site/dist/ from the demo registry
	@test -f data/demo-run/registry.sqlite || ccc demo --no-evals
	ccc site build --db data/demo-run/registry.sqlite --evals data/demo-run/evals/runs --release data/demo-run/release

eval:  ## offline eval suites on replay fixtures, then the regression gates
	@set -e; for s in $(SUITES); do ccc eval run --suite $$s --mode replay --no-store; done
	@set -e; for s in $(SUITES); do ccc eval gate --suite $$s; done

schema:  ## regenerate schemas/open-fap-schema.json
	ccc schema export

clean:  ## remove generated outputs (keeps the committed corpus and fixtures)
	rm -rf data/demo-run data/release site/dist packet-demo-out .pytest_cache .ruff_cache
