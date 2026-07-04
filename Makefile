# Demo runbook targets. Three long-running pieces run in separate
# terminals: mcp (store), monitor (prober), orchestrate (pipeline).

PYTHON ?= .venv/bin/python
PROJECT ?= candidate-app

include .env
-include config/projects/$(PROJECT)/.env
export

seed:
	$(PYTHON) scripts/seed.py --project $(PROJECT)

mcp:
	$(PYTHON) -m mcp_server.server

monitor:
	$(PYTHON) monitor/synthetic_monitor.py --url $$($(PYTHON) tools/deploy.py url)

orchestrate:
	$(PYTHON) orchestrator.py

deploy-baseline:
	$(PYTHON) tools/deploy.py baseline

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate deploy-baseline test
