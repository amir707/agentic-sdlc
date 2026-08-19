"""The core's outward-facing ports (ADR-0007, completed).

The engine coordinates through three external things — the repo host
(the PR is the artifact), the delivery store (the single source of
lifecycle truth, G3) and the deploy tool — plus the agent invoker and
the two executors already ported in invoker.py / executor.py. Until
now those three were CONCRETE types on RunContext (GitHubRepoHost,
DeliveryStore, the sdlc.adapters.gcloud module), so the core imported the
adapters it was supposed to be independent of, and the ADK adapter
imported the core back: a cycle in spirit.

These Protocols name exactly the surface the core uses — no more. The
adapters satisfy them structurally (no inheritance needed); tests hand
in fakes without monkeypatching modules; the composition root is the
only place a concrete is chosen. The two error types cross the port,
so they are defined HERE and the adapters raise them (adapter -> core,
the right direction).
"""

from typing import Any, Protocol


class RepoHostError(Exception):
    """The repo host refused or could not complete a call (HTTP error,
    missing PR after a repo reset, ...). Callers escalate the ONE item
    it concerns; it never aborts a pass."""


class DeployError(RuntimeError):
    """A deploy/traffic command failed (redacted command + error in the
    message). Callers treat this as an infrastructure failure of ONE
    stage, never a reason to kill a whole run."""


class RepoHost(Protocol):
    """What the engine needs from the code host (GitHub today)."""

    def open_pr(self, head: str, title: str, body: str) -> int: ...
    def find_open_pr(self, head: str) -> int | None: ...
    def get_pr(self, pr: int) -> dict: ...          # head_sha, head_ref, mergeable, ...
    def get_review_threads(self, pr: int) -> list[dict]: ...
    def post_comment(self, pr: int, body: str) -> None: ...
    def update_title(self, pr: int, title: str) -> None: ...
    def merge_pr(self, pr: int) -> str: ...
    def close_pr(self, pr: int) -> None: ...
    def authenticated_remote(self) -> str: ...


class Store(Protocol):
    """One tool call against the delivery store's MCP surface, under
    whatever role the handle carries. The store's tool names ARE the
    contract (mcp_server/server.py); the core never sees SQL."""

    async def call(self, tool: str, **args: Any) -> Any: ...


class Deployer(Protocol):
    """Deploying, both halves: build + tag a traffic-less preprod
    revision for a PR (returns its URL), and promote a tag to 100%."""

    def deploy_preprod(self, pr: int, source_dir: str | None = None) -> str: ...
    def promote(self, tag: str) -> None: ...
