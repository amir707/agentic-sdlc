# Demo runbook targets. Three long-running pieces run in separate
# terminals: mcp (store), monitor (prober), orchestrate (pipeline).

PYTHON := .venv/bin/python
PROJECT ?= candidate-app
PARALLEL ?= 1

include .env
-include projects-config/$(PROJECT)/.env
export

seed:
	$(PYTHON) scripts/seed.py --project $(PROJECT)

mcp:
	$(PYTHON) -m mcp_server.server

monitor:
	$(PYTHON) -m sdlc_steps.monitor --url $$($(PYTHON) -m adapters.deploy url)

orchestrate:
	$(PYTHON) -m orchestrator --project $(PROJECT) --parallel $(PARALLEL)

deploy-baseline:
	$(PYTHON) -m adapters.deploy baseline

demo:
	bash scripts/demo.sh

status:
	$(PYTHON) scripts/store_status.py

# live store view: refreshes every 5s (4th terminal during demos)
watch:
	while true; do clear; $(PYTHON) scripts/store_status.py; sleep 5; done

verify-demo:
	$(PYTHON) scripts/verify_demo.py

adk-web:
	.venv/bin/adk web tests/debug/adk_web

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate deploy-baseline demo status watch verify-demo adk-web test
