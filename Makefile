# Demo runbook targets. Three long-running pieces run in separate
# terminals: mcp (store), monitor (prober), orchestrate (pipeline).

PYTHON ?= .venv/bin/python
PROJECT ?= candidate-app

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
	$(PYTHON) -m orchestrator --project $(PROJECT)

deploy-baseline:
	$(PYTHON) -m adapters.deploy baseline

adk-web:
	.venv/bin/adk web tests/debug/adk_web

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate deploy-baseline adk-web test
