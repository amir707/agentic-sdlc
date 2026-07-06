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
reset-demo:
	bash scripts/reset_demo.sh

# surgical replay of ONE item: make reset-item ITEM=PAY-102
reset-item:
	$(PYTHON) scripts/reset_item.py --item $(ITEM) --project $(PROJECT)

demo:
	bash scripts/demo.sh

# local store: read the SQLite file directly; cloud store (run with
# DELIVERY_STORE_URL=https://.../mcp): curl its /status route
status:
	@if [ -n "$$DELIVERY_STORE_URL" ]; then \
	  curl -fsS -H "Authorization: Bearer $$MCP_TOKEN_MONITOR" \
	    "$${DELIVERY_STORE_URL%/mcp}/status"; \
	else \
	  $(PYTHON) scripts/store_status.py; \
	fi

# live store view: refreshes every 5s (4th terminal during demos)
watch:
	@while true; do \
	  out=$$($(MAKE) -s status 2>&1); \
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

.PHONY: seed mcp monitor orchestrate deploy-baseline reset-demo reset-item demo status watch verify-demo adk-web test
