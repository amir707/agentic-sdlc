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

import os

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


def build_llm_agent(spec: AgentSpec, meter=None,
                    max_output_tokens: int | None = None) -> LlmAgent:
    """Materialize a neutral AgentSpec into an ADK LlmAgent.

    Also used by the adk_web/ dev entries, so interactive debugging
    exercises exactly the agent the pipeline runs."""
    if spec.output_schema and spec.tools:
        raise ValueError(
            f"{spec.name}: output_schema and tools are mutually exclusive "
            "on LLM agents — tool-using agents return JSON text instead")
    return LlmAgent(
        name=spec.name,
        model=_resolve_model(spec.model),
        instruction=spec.instruction,
        tools=[_materialize_tool(t) for t in spec.tools],
        output_schema=spec.output_schema,
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
