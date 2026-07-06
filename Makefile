# Demo runbook targets. Three long-running pieces run in separate
# terminals: mcp (store), monitor (prober), orchestrate (pipeline).

PYTHON := .venv/bin/python
PROJECT ?= candidate-app
PARALLEL ?= 1

include .env
-include projects-config/$(PROJECT)/.env
export

# One world per governed project: each project's store is its own file,
# derived from PROJECT so no one has to know the convention. Pin
# DELIVERY_STORE_DB in .env (or the shell) only to override.
ifeq ($(strip $(DELIVERY_STORE_DB)),)
DELIVERY_STORE_DB = $(if $(filter candidate-app,$(PROJECT)),delivery_store.sqlite3,delivery_store-$(PROJECT).sqlite3)
endif

# World-selecting targets never guess which project you mean: PROJECT
# must come from the command line or the shell, not the default.
define require_project
	@if [ "$(origin PROJECT)" = "file" ]; then \
	  echo "PROJECT is required: make $@ PROJECT=<name>   (available: $$(ls projects-config | tr '\n' ' '))"; \
	  exit 1; \
	fi
endef

seed:
	$(require_project)
	$(PYTHON) scripts/seed.py --project $(PROJECT)

mcp:
	$(require_project)
	$(PYTHON) -m mcp_server.server

monitor:
	$(PYTHON) -m sdlc_steps.monitor --url $$($(PYTHON) -m adapters.deploy url)

orchestrate:
	$(require_project)
	$(PYTHON) -m orchestrator --project $(PROJECT) --parallel $(PARALLEL)

deploy-baseline:
	PROJECT_CHECKOUT_DIR=$$($(PYTHON) -m orchestrator.provisioning --project $(PROJECT)) \
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

# live store view: refreshes every 5s, colorized, changed lines get a
# yellow margin bar (4th terminal during demos)
watch:
	@$(PYTHON) scripts/watch.py

# preview new-project onboarding: scaffold a bundle interactively,
# show what was generated, then choose to keep or delete it
try-setup:
	@test -n "$(NAME)" || { echo "usage: make try-setup NAME=my-app"; exit 1; }
	@python3 scripts/setup.py --project $(NAME) --scaffold-only
	@echo "-- generated files:" && find projects-config/$(NAME) -type f | sort
	@printf "keep the bundle? [y/N] " && read keep; \
	  if [ "$$keep" = "y" ]; then \
	    echo "kept — continue: python3 scripts/setup.py --project $(NAME)"; \
	  else rm -rf projects-config/$(NAME) && echo "deleted (preview only)"; fi

verify-demo:
	$(PYTHON) scripts/verify_demo.py

adk-web:
	$(PYTHON) -m google.adk.cli web tests/debug/adk_web \
	  --session_service_uri $${ADK_SESSIONS_DB:-sqlite+aiosqlite:///.adk_sessions.db}

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate deploy-baseline reset-demo reset-item demo status watch try-setup verify-demo adk-web test
