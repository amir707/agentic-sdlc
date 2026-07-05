"""Lenient JSON extraction from model output.

Agents that use tools cannot also use ADK's output_schema (the two are
mutually exclusive on LlmAgent), so structured verdicts come back as
text. Models wrap JSON in prose or code fences; this strips that
reliably and fails loudly when there is no JSON at all.
"""

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | list:
    fenced = _FENCE.search(text)
    if fenced:
        return json.loads(fenced.group(1))
    # First balanced {...} block in the text.
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"unbalanced JSON in model output: {text[:200]!r}")
