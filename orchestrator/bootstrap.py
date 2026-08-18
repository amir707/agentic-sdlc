"""The composition root, in one place (ADR-0007).

Six entry points used to repeat the same wiring — load the engine .env
then the project's, parse --project/--debug, pick the ADK adapters,
build the RunContext, summarize a top-level failure, or stand up the
resident ADK api server with its heartbeat. Each copy drifted a little.
This module is that wiring, once; the entry points keep only what is
unique to them (their arguments, the coroutine they run, the trigger
path they serve).

It is the ONLY module in orchestrator/ that names a framework or a
concrete adapter (test_framework_boundary lists it as a composition
root). Everything it hands out is typed to the ports.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from orchestrator.config import ProjectConfig, load_project
from orchestrator.context import RunContext
from orchestrator.executor import PipelineExecutor, ReleaseExecutor
from orchestrator.invoker import AgentInvoker

ROOT = Path(__file__).resolve().parent.parent


# --- environment + arguments -------------------------------------------------

def load_env(project: str) -> None:
    """Engine secrets first (model keys, store role tokens), then the
    project's own (repo PAT, GCP target). Missing files are no-ops; a
    variable already in the environment (Cloud Run secrets) wins."""
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / project / ".env")


def parser(description: str) -> argparse.ArgumentParser:
    """The base every entry point extends: --project and --debug."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--project", required=True)
    p.add_argument("--debug", action="store_true",
                   help="show full tracebacks instead of one-line "
                        "failure summaries")
    return p


# --- wiring ---------------------------------------------------------------------

def build_context(project: ProjectConfig, invoker: AgentInvoker,
                  executor: PipelineExecutor | None = None,
                  release_executor: ReleaseExecutor | None = None
                  ) -> RunContext:
    """Assemble one run's handles behind the ports. Each entry point
    injects only what it runs (release leaves the per-item executor
    None). The working checkout is PROVISIONED here (cloned into
    scratch, healed if missing) — no pre-existing local copy needed."""
    from adapters import deploy
    from adapters.repo_host import GitHubRepoHost
    from adapters.store_client import DeliveryStore
    from orchestrator import provisioning

    repo_host = GitHubRepoHost(project.repo, os.environ["GITHUB_TOKEN"])
    workspace = provisioning.provision(
        project.name, repo_host.authenticated_remote())
    return RunContext(
        project=project,
        store=DeliveryStore.for_agents(),
        repo_host=repo_host,
        invoker=invoker,
        workspace=workspace,
        executor=executor,
        release_executor=release_executor,
        deployer=deploy,
        resolver_store=DeliveryStore.for_resolver(),
    )


def sprint_context(project_name: str) -> RunContext:
    """The sprint's wiring: agents on ADK, the per-item pipeline as an
    ADK Workflow, and the release Workflow for the trickle pass."""
    from adapters.adk.executor import ADKPipelineExecutor
    from adapters.adk.invoker import ADKInvoker
    from adapters.adk.release_workflow import ADKReleaseExecutor
    return build_context(load_project(project_name), invoker=ADKInvoker(),
                         executor=ADKPipelineExecutor(),
                         release_executor=ADKReleaseExecutor())


def release_context(project_name: str) -> RunContext:
    """The release side's wiring: no per-item executor — release runs
    only the release-manager agent and the deterministic merge gate."""
    from adapters.adk.invoker import ADKInvoker
    from adapters.adk.release_workflow import ADKReleaseExecutor
    return build_context(load_project(project_name), invoker=ADKInvoker(),
                         release_executor=ADKReleaseExecutor())


def announce_models() -> None:
    print("[orchestrator] models: "
          f"coder={os.environ.get('CODER_MODEL', 'anthropic/claude-sonnet-5')} | "
          f"reviewer={os.environ.get('REVIEWER_MODEL') or os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')} | "
          f"gemini-default={os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')}",
          flush=True)


# --- running ----------------------------------------------------------------------

def run_cli(coro, *, label: str, interrupted: str, failed_hint: str = "",
            debug: bool = False) -> None:
    """Run one top-level coroutine with the operator-facing failure
    contract: Ctrl-C exits 130 with a resume hint; any other exception
    is summarized on one line (orchestrator/errors.py) unless --debug."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print(f"\n[{label}] interrupted — {interrupted}", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — top level: summarize
        if debug:
            raise
        from orchestrator.errors import one_line
        print(f"\n[{label}] FAILED: {one_line(exc)}", file=sys.stderr)
        if failed_hint:
            print(f"[{label}] {failed_hint}", file=sys.stderr)
        sys.exit(1)


def serve_resident(app_dir: str, app_name: str, host: str, port: int,
                   heartbeat_minutes: float, project: str,
                   describe: str) -> None:
    """Stand up a resident ADK api server (ambient Pub/Sub trigger) with
    the internal heartbeat as a sibling task. Single-flight by default:
    two concurrent events must queue, not race two passes."""
    os.environ.setdefault("ADK_TRIGGER_MAX_CONCURRENT", "1")
    from google.adk.cli.fast_api import get_fast_api_app
    from orchestrator.heartbeat import serve_with_heartbeat
    trigger_path = f"/apps/{app_name}/trigger/pubsub"
    app = get_fast_api_app(agents_dir=str(ROOT / "adapters" / "adk" / app_dir),
                           web=False, trigger_sources=["pubsub"])
    print(f"[{app_name}-service] {project}: awake on http://{host}:{port} — "
          f"{describe} (POST {trigger_path})", flush=True)
    serve_with_heartbeat(app, host, port, trigger_path, heartbeat_minutes,
                         app_name)
