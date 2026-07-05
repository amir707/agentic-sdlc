"""Agent-layer tests that need no API keys: spec wiring, workspace
sandbox guarantees, GitHub adapter contract (mock transport), JSON
extraction, and invoker model routing."""

import json

import httpx
import pytest

from adapters.adk.invoker import _resolve_model
from adapters.repo_host import GitHubRepoHost, RepoHostError
from orchestrator.config import load_project
from orchestrator.invoker import StoreTools
from orchestrator.json_util import extract_json
from tools.fs_tools import make_workspace_tools


# --- workspace sandbox -------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "payments.py").write_text("x = 1\n")
    (tmp_path / "app" / "chaos.py").write_text("rigging\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret\n")
    return tmp_path


def _tool(tools, name):
    return next(t for t in tools if t.__name__ == name)


def test_workspace_read_write_roundtrip(workspace):
    tools = make_workspace_tools(workspace)
    _tool(tools, "write_file")("app/catalog.py", "y = 2\n")
    assert _tool(tools, "read_file")("app/catalog.py") == "y = 2\n"
    files = _tool(tools, "list_files")()
    assert "app/catalog.py" in files and ".git/config" not in str(files)


def test_workspace_blocks_escape_and_rigging(workspace):
    tools = make_workspace_tools(workspace)
    write = _tool(tools, "write_file")
    with pytest.raises(ValueError):
        write("../outside.txt", "nope")
    with pytest.raises(ValueError):
        write(".git/hooks/post-commit", "nope")
    with pytest.raises(ValueError):
        write("app/chaos.py", "nope")           # governance rigging
    with pytest.raises(ValueError):
        _tool(tools, "read_file")(".git/config")


# --- agent specs -------------------------------------------------------------

def test_specs_compose_prompts_and_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_AGENTS", "t-agents")
    monkeypatch.setenv("CODER_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "gemini-flash-latest")
    from sdlc_steps.code_reviewer import spec as reviewer_spec
    from sdlc_steps.coder import spec as coder_spec
    from sdlc_steps.release_manager import spec as rm_spec

    project = load_project("candidate-app")
    coder = coder_spec.build(project, str(tmp_path))
    assert coder.model.startswith("anthropic/")
    assert "Core rules" in coder.instruction
    assert "candidate-app customised prompt" in coder.instruction  # overlay applied

    reviewer = reviewer_spec.build(project, str(tmp_path), "diff text")
    assert reviewer.model.startswith("gemini")
    # read-only workspace: no write_file, no run_tests, but has analyze_diff
    names = {t.__name__ for t in reviewer.tools}
    assert names == {"list_files", "read_file", "analyze_diff"}

    rm = rm_spec.build(project)
    # narrow store surface only, DECLARED not constructed (ADR-0007)
    assert len(rm.tools) == 1
    assert isinstance(rm.tools[0], StoreTools)


def test_invoker_routes_models():
    assert _resolve_model("gemini-flash-latest") == "gemini-flash-latest"
    litellm_model = _resolve_model("anthropic/claude-sonnet-5")
    assert type(litellm_model).__name__ == "LiteLlm"


# --- json extraction ---------------------------------------------------------

def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('verdict:\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert extract_json('text {"a": {"b": 2}} trailing {"c":3}') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


# --- GitHub adapter (mock transport) ----------------------------------------

def _github_mock(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    if path.endswith("/pulls") and method == "POST":
        body = json.loads(request.content)
        assert body["head"] == "item/PAY-101-refund-totals"
        return httpx.Response(201, json={"number": 7})
    if path.endswith("/issues/7/comments") and method == "POST":
        return httpx.Response(201, json={"id": 1})
    if path.endswith("/pulls/7") and method == "GET":
        if request.headers["accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text="diff --git a/x b/x")
        return httpx.Response(200, json={
            "number": 7, "title": "t", "body": "Item: PAY-101",
            "state": "open", "merged": False,
            "head": {"ref": "item/PAY-101-refund-totals", "sha": "abc123"}})
    if path.endswith("/issues/7/comments") and method == "GET":
        return httpx.Response(200, json=[
            {"user": {"login": "amir707"}, "body": "/approve",
             "created_at": "2026-07-05T00:00:00Z"}])
    if path.endswith("/pulls/7/merge") and method == "PUT":
        return httpx.Response(200, json={"sha": "merged123"})
    if path.endswith("/pulls/7") and method == "PATCH":
        return httpx.Response(200, json={})
    return httpx.Response(404, json={"message": f"unmocked {method} {path}"})


def test_repo_host_contract():
    host = GitHubRepoHost("amir707/candidate-app", "test-token",
                          transport=httpx.MockTransport(_github_mock))
    pr = host.open_pr("item/PAY-101-refund-totals", "Add refund totals",
                      "Item: PAY-101")
    assert pr == 7
    host.post_comment(7, "dossier")
    assert host.get_diff(7).startswith("diff --git")
    threads = host.get_review_threads(7)
    assert threads[0]["author"] == "amir707" and threads[0]["body"] == "/approve"
    assert host.merge_pr(7) == "merged123"
    host.update_title(7, "[area:payments][risk:high][flag:yes] Add refund totals")
    assert host.get_pr(7)["head_sha"] == "abc123"
    assert "x-access-token:test-token@" in host.authenticated_remote()


def test_repo_host_raises_on_error():
    host = GitHubRepoHost("amir707/candidate-app", "test-token",
                          transport=httpx.MockTransport(_github_mock))
    with pytest.raises(RepoHostError):
        host.post_comment(99, "nope")


def test_rate_limit_detection_and_backoff():
    from adapters.adk.invoker import _is_rate_limit, _retry_seconds

    gemini_429 = Exception(
        "429 RESOURCE_EXHAUSTED. ... Please retry in 8.478154025s.")
    assert _is_rate_limit(gemini_429)
    assert 10 <= _retry_seconds(gemini_429, 0) <= 11  # provider hint + margin

    anthropic_429 = Exception("RateLimitError: rate_limit_error ...")
    assert _is_rate_limit(anthropic_429)
    assert _retry_seconds(anthropic_429, 1) == 30.0   # exponential fallback

    assert not _is_rate_limit(Exception("400 INVALID_ARGUMENT"))
