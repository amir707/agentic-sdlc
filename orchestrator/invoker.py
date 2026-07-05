"""AgentInvoker port and its single implementation, ADKInvoker.

The port keeps the core framework-neutral: an agent is described by an
AgentSpec (instruction, model name, tools) and invoked with a text
payload; the result carries the final text plus token usage. ALL
ADK-specific wiring (LlmAgent, Runner, sessions, LiteLLM bridging)
lives here and nowhere else — core code never imports ADK.

Agents are invocations, not daemons: each invoke() builds a fresh
agent + in-memory session, runs it to completion, and throws both away.
State lives in GitHub and the delivery store, never in the agent.
"""

import os
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


@dataclass
class AgentSpec:
    """Framework-neutral agent description."""
    name: str
    instruction: str
    model: str                       # "gemini-*" native, else via LiteLLM
    tools: list = field(default_factory=list)


@dataclass
class Invocation:
    text: str
    input_tokens: int
    output_tokens: int


class ADKInvoker:
    def __init__(self, max_output_tokens: int | None = None):
        # Hard cost cap per invocation (bounded loops everywhere).
        self.max_output_tokens = max_output_tokens or int(
            os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "8192"))

    def _resolve_model(self, model: str):
        # Gemini is ADK-native; anything else goes through LiteLLM
        # (e.g. anthropic/claude-*), normalizing usage metadata too.
        return model if model.startswith("gemini") else LiteLlm(model=model)

    async def invoke(self, spec: AgentSpec, message: str,
                     max_steps: int = 30) -> Invocation:
        agent = LlmAgent(
            name=spec.name,
            model=self._resolve_model(spec.model),
            instruction=spec.instruction,
            tools=list(spec.tools),
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=self.max_output_tokens),
        )
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="agentic-sdlc",
                        session_service=session_service)
        session = await session_service.create_session(
            app_name="agentic-sdlc", user_id="orchestrator")

        content = types.Content(role="user", parts=[types.Part(text=message)])
        final_text: list[str] = []
        input_tokens = output_tokens = 0
        steps = 0
        async for event in runner.run_async(
                user_id="orchestrator", session_id=session.id,
                new_message=content):
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"{spec.name}: exceeded {max_steps} steps (runaway loop guard)")
            usage = getattr(event, "usage_metadata", None)
            if usage:
                input_tokens += usage.prompt_token_count or 0
                output_tokens += usage.candidates_token_count or 0
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if getattr(part, "text", None):
                        final_text.append(part.text)

        return Invocation(text="\n".join(final_text).strip(),
                          input_tokens=input_tokens,
                          output_tokens=output_tokens)
