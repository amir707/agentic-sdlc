"""ADKInvoker — the AgentInvoker port's one implementation (ADK 2).

ALL ADK-specific wiring (LlmAgent, Runner, sessions, LiteLLM bridging,
MCP toolsets, callbacks) lives here and nowhere else; the SDLC core
only ever sees AgentSpec in and Invocation out (ADR-0007).

Each invoke() builds a fresh LlmAgent + in-memory session, runs it to
completion, and throws both away. Token metering uses the idiomatic
after_model_callback (fires once per model turn, LiteLLM-normalized
usage included), with the event-scan kept as a fallback for models
that only report usage on events.
"""

import asyncio
import os
import re

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.genai import types

from orchestrator.invoker import AgentSpec, Invocation, StoreTools


def _materialize_tool(tool):
    """Turn a declared tool need into an ADK tool."""
    if isinstance(tool, StoreTools):
        port = os.environ.get("DELIVERY_STORE_PORT", "8787")
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization":
                         f"Bearer {os.environ['MCP_TOKEN_AGENTS']}"},
            ),
            tool_filter=list(tool.tool_filter),
        )
    return tool  # plain callable -> ADK wraps it as a function tool


def _resolve_model(model: str):
    # Gemini is ADK-native; anything else goes through LiteLLM
    # (e.g. anthropic/claude-*), which also normalizes usage metadata.
    return model if model.startswith("gemini") else LiteLlm(model=model)


class MinIntervalLimiter:
    """Proactive request pacing: free-tier Gemini enforces N requests
    per MINUTE per model, and one agent invocation is several model
    turns — so pacing must happen per REQUEST (before_model_callback),
    not per invocation. Shared across all Gemini agents in the process
    (the quota is per project, not per agent)."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            due = self._last + self.min_interval
            now = loop.time()
            if due > now:
                await asyncio.sleep(due - now)
            self._last = asyncio.get_running_loop().time()


def _gemini_limiter() -> MinIntervalLimiter:
    """GEMINI_RPM env (default 12, safely under the 15/min free tier);
    set GEMINI_RPM=0 to disable when billing is enabled."""
    global _LIMITER
    if _LIMITER is None:
        rpm = float(os.environ.get("GEMINI_RPM", "12"))
        _LIMITER = MinIntervalLimiter(60.0 / rpm if rpm > 0 else 0.0)
    return _LIMITER


_LIMITER: MinIntervalLimiter | None = None


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text \
        or "rate_limit" in text.lower()


def _retry_seconds(exc: BaseException, attempt: int) -> float:
    """Honor the provider's 'retry in Ns' hint when present, else back
    off exponentially. Free-tier Gemini is 5 requests/minute, so waits
    in the tens of seconds are normal, not a hang."""
    match = re.search(r"retry in ([0-9.]+)s", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1)) + 2.0  # small margin past the window
    return min(15.0 * (2 ** attempt), 120.0)


def build_llm_agent(spec: AgentSpec, meter=None,
                    max_output_tokens: int | None = None) -> LlmAgent:
    """Materialize a neutral AgentSpec into an ADK LlmAgent.

    Also used by the adk_web/ dev entries, so interactive debugging
    exercises exactly the agent the pipeline runs."""
    if spec.output_schema and spec.tools:
        raise ValueError(
            f"{spec.name}: output_schema and tools are mutually exclusive "
            "on LLM agents — tool-using agents return JSON text instead")

    throttle = None
    if spec.model.startswith("gemini"):
        async def throttle(callback_context, llm_request):
            await _gemini_limiter().wait()
            return None

    return LlmAgent(
        name=spec.name,
        model=_resolve_model(spec.model),
        instruction=spec.instruction,
        tools=[_materialize_tool(t) for t in spec.tools],
        output_schema=spec.output_schema,
        before_model_callback=throttle,
        after_model_callback=meter,
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=max_output_tokens or int(
                os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "8192"))),
    )


class ADKInvoker:
    def __init__(self, max_output_tokens: int | None = None):
        # Hard cost cap per invocation (bounded loops everywhere).
        self.max_output_tokens = max_output_tokens

    async def invoke(self, spec: AgentSpec, message: str,
                     max_steps: int = 30) -> Invocation:
        """Invoke with rate-limit resilience: a 429 anywhere in the run
        retries the WHOLE invocation after the provider's suggested
        wait. Safe because agents are stateless invocations — a retry
        is just a fresh run (store writes are idempotent-by-latest,
        workspace edits are resumed by the same agent)."""
        retries = int(os.environ.get("AGENT_RATE_LIMIT_RETRIES", "5"))
        for attempt in range(retries + 1):
            try:
                return await self._invoke_once(spec, message, max_steps)
            except Exception as exc:  # noqa: BLE001 — filtered below
                if not _is_rate_limit(exc) or attempt >= retries:
                    raise
                if "PerDay" in str(exc):
                    # A DAILY quota will not recover within any retry
                    # window — fail fast with the actionable summary.
                    raise
                delay = _retry_seconds(exc, attempt)
                print(f"[invoker] {spec.name}: rate-limited (429); "
                      f"retry {attempt + 1}/{retries} in {delay:.0f}s",
                      flush=True)
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def _invoke_once(self, spec: AgentSpec, message: str,
                           max_steps: int) -> Invocation:
        usage = {"input": 0, "output": 0}

        def meter(callback_context, llm_response):
            meta = getattr(llm_response, "usage_metadata", None)
            if meta:
                usage["input"] += meta.prompt_token_count or 0
                usage["output"] += meta.candidates_token_count or 0
            return None  # never alter the response

        agent = build_llm_agent(spec, meter=meter,
                                max_output_tokens=self.max_output_tokens)
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="agentic-sdlc",
                        session_service=session_service)
        session = await session_service.create_session(
            app_name="agentic-sdlc", user_id="orchestrator")

        content = types.Content(role="user", parts=[types.Part(text=message)])
        final_text: list[str] = []
        event_input = event_output = 0
        steps = 0
        async for event in runner.run_async(
                user_id="orchestrator", session_id=session.id,
                new_message=content):
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"{spec.name}: exceeded {max_steps} steps (runaway guard)")
            meta = getattr(event, "usage_metadata", None)
            if meta:
                event_input += meta.prompt_token_count or 0
                event_output += meta.candidates_token_count or 0
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if getattr(part, "text", None):
                        final_text.append(part.text)

        return Invocation(
            text="\n".join(final_text).strip(),
            input_tokens=max(usage["input"], event_input),
            output_tokens=max(usage["output"], event_output))
