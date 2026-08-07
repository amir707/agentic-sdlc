# Demo runbook targets. Three long-running pieces run in separate
# terminals: mcp (store), monitor (prober), orchestrate (pipeline).

PYTHON := .venv/bin/python
PROJECT ?= candidate-app
PARALLEL ?= 1
HEARTBEAT ?= 5
RELEASE_URL ?=

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
# must come from the command line or the shell, not the default. Only
# demo/reset-demo keep the default — they run candidate-app's scripted
# demo rig by definition.
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
	$(require_project)
	$(PYTHON) -m sdlc_steps.monitor --project $(PROJECT) \
	  --url $$($(PYTHON) -m adapters.deploy url)

orchestrate:
	$(require_project)
	$(PYTHON) -m orchestrator --project $(PROJECT) --parallel $(PARALLEL)

# ONE release pass over store state (Workstream B): the event-driven unit
# of release, independent of a sprint run. Repeated invocation is the
# trigger's job (Cloud Scheduler / webhook in the cloud; cron/loop locally).
release:
	$(require_project)
	$(PYTHON) -m orchestrator.release --project $(PROJECT)

# The RESIDENT release manager: stays awake listening; runs one release
# pass per event (Pub/Sub push in the cloud, HTTP POST locally).
release-service:
	$(require_project)
	$(PYTHON) -m orchestrator.release_service --project $(PROJECT) --heartbeat-minutes $(HEARTBEAT)

# The RESIDENT sprint orchestrator: stays awake listening; one sprint
# resume pass per event (each awaiting gate gets exactly one look).
# RELEASE_URL=http://127.0.0.1:8788/apps/release/trigger/pubsub delegates
# release to the resident release service (its log owns release narration).
orchestrate-service:
	$(require_project)
	$(PYTHON) -m orchestrator.sprint_service --project $(PROJECT) --parallel $(PARALLEL) --heartbeat-minutes $(HEARTBEAT) --release-url "$(RELEASE_URL)"

deploy-baseline:
	$(require_project)
	PROJECT_CHECKOUT_DIR=$$($(PYTHON) -m orchestrator.provisioning --project $(PROJECT)) \
	  $(PYTHON) -m adapters.deploy baseline

# FULL demo reset: candidate-app main + branches + baseline traffic + store
reset-demo:
	bash scripts/reset_demo.sh

# surgical replay of ONE item: make reset-item ITEM=PAY-102
reset-item:
	$(require_project)
	$(PYTHON) scripts/reset_item.py --item $(ITEM) --project $(PROJECT)

demo:
	PROJECT=$(PROJECT) bash scripts/demo.sh

# local store: read the SQLite file directly; cloud store (run with
# DELIVERY_STORE_URL=https://.../mcp): curl its /status route
status:
	$(require_project)
	@if [ -n "$$DELIVERY_STORE_URL" ]; then \
	  curl -fsS -H "Authorization: Bearer $$MCP_TOKEN_MONITOR" \
	    "$${DELIVERY_STORE_URL%/mcp}/status"; \
	else \
	  $(PYTHON) scripts/store_status.py; \
	fi

# live store view: refreshes every 5s, colorized, changed lines get a
# yellow margin bar (4th terminal during demos)
watch:
	$(require_project)
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
	$(require_project)
	$(PYTHON) scripts/verify_demo.py

adk-web:
	$(PYTHON) -m google.adk.cli web tests/debug/adk_web \
	  --session_service_uri $${ADK_SESSIONS_DB:-sqlite+aiosqlite:///.adk_sessions.db}

test:
	$(PYTHON) -m pytest -q

.PHONY: seed mcp monitor orchestrate orchestrate-service release release-service deploy-baseline reset-demo reset-item demo status watch try-setup verify-demo adk-web test
