"""Agent-facing tools — the coder's ENTIRE effect surface (guardrail G2):

  fs_tools.py       the sandboxed workspace (list/read/write/delete under
                    the checkout, denylisted paths, .git engine-enforced);
                    errors are returned as strings, never raised
  diff_analysis.py  files_touched(diff): the deterministic input to
                    review, verify and blast radius

Anything with git, network, cloud or GitHub credentials lives in the
engine, never here — the missing tool IS the guardrail.
"""
