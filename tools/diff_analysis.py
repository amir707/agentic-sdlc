"""Diff analysis (deterministic tool, NOT an agent).

Parses a unified diff into the facts downstream steps need: files
touched, areas touched (via the project's area map), and feature-flag
coverage (did the diff gate new behavior behind a flag). No judgment
here — verify and the reviewer judge; this just measures.
"""

import re

# unified diff header: +++ b/<path>  (or /dev/null for deletions)
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
# a flag key added to flags.json: +  "flag_name": false
_FLAG_DEFINED = re.compile(r'^\+\s*"([a-z0-9_]+)"\s*:', re.MULTILINE)
# The flag-usage IDIOM belongs to the governed repo; the engine only
# recognizes the mechanical signature, in either shape:
#   direct:   is_enabled("flag_name") / flags.enabled('x') / flag_on("x")
#   indirect: FLAG = "flag_name" ... is_enabled(FLAG)  — one constant hop
_FLAG_USED = re.compile(
    r'^\+.*\b\w*(?:enabled|flag)\w*\s*\(\s*["\']([a-z0-9_]+)["\']',
    re.MULTILINE | re.IGNORECASE)
_FLAG_LOOKUP_CALL = re.compile(
    r'^\+.*\b\w*(?:enabled|flag)\w*\s*\(', re.MULTILINE | re.IGNORECASE)
_ADDED_STRING = re.compile(r'^\+.*?["\']([a-z0-9_]+)["\']', re.MULTILINE)


def files_touched(diff_text: str) -> list[str]:
    return [f for f in _DIFF_FILE.findall(diff_text) if f != "/dev/null"]


def areas_touched(diff_text: str, project) -> set[str]:
    """Areas the diff touches, via ProjectConfig.area_for."""
    return {project.area_for(f) for f in files_touched(diff_text)}


def flag_coverage(diff_text: str) -> dict:
    """Deterministic flag facts about a diff.

    covered means: at least one flag is BOTH defined in flags.json and
    checked in added code — the mechanical signature of "new behavior
    gated behind a flag". Whether coverage is *required* is policy
    (flag_required_min_risk), applied by the reviewer and verify.
    """
    in_flags_json = _in_file_hunks(diff_text, "flags.json")
    outside = _outside_file_hunks(diff_text, "flags.json")
    defined = set(_FLAG_DEFINED.findall(in_flags_json))
    used = set(_FLAG_USED.findall(diff_text))
    if _FLAG_LOOKUP_CALL.search(outside):
        # a lookup call exists; a defined name held in a constant that
        # feeds it counts as usage (regexes cannot trace dataflow, so
        # "defined + referenced + looked-up" is the honest bar)
        used |= set(_ADDED_STRING.findall(outside))
    gated = defined & used
    return {
        "flags_defined": sorted(defined),
        "flags_used": sorted(used),
        "covered": bool(gated),
        "gated_flags": sorted(gated),
    }


def _in_file_hunks(diff_text: str, filename: str) -> str:
    """The portion of a unified diff belonging to one file."""
    parts = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
    return "".join(p for p in parts if p.startswith(f"a/{filename} ")
                   or f"+++ b/{filename}" in p)


def _outside_file_hunks(diff_text: str, filename: str) -> str:
    """Everything in the diff EXCEPT one file's hunks."""
    parts = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
    return "".join(p for p in parts if not (p.startswith(f"a/{filename} ")
                                            or f"+++ b/{filename}" in p))
