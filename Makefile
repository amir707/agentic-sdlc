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
	CANDIDATE_APP_DIR=$$($(PYTHON) -m orchestrator.provisioning --project $(PROJECT)) \
	  $(PYTHON) -m adapters.deploy baseline

# FULL demo reset: candidate-app main + branches + baseline traffic + store
reset:
	bash scripts/reset_demo.sh

# surgical replay of ONE item: make reset-item ITEM=PAY-102
reset-item:
	$(PYTHON) scripts/reset_item.py --item $(ITEM) --project $(PROJECT)

demo:
	bash scripts/demo.sh

status:
	$(PYTHON) scripts/store_status.py

# live store view: refreshes every 5s (4th terminal during demos)
watch:
	@while true; do \
	  out=$$($(PYTHON) scripts/store_status.py 2>&1); \
	  printf '\033[H\033[2J\033[3J'; printf '%s\n' "$$out"; \
	  sleep 5; \
	done

verify-demo:
	$(PYTHON) scripts/verify_demo.py

adk-web:
	$(PYTHON) -m google.adk.cli web tests/debug/adk_web \
	  --session_service_uri $${ADK_SESSIONS_DB:-sqlite+aiosqlite:///.adk_sessions.db}

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate deploy-baseline reset reset-item demo status watch verify-demo adk-web test
