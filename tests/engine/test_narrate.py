"""sdlc.engine.narrate: one voice, two envelopes."""

import json
import pathlib
import re

from sdlc.engine.narrate import say
from sdlc.engine.paths import REPO_ROOT


def test_human_format_is_the_familiar_tagged_line(capsys, monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    say("release", "MERGED PR #7", pr=7)
    assert capsys.readouterr().out == "[release] MERGED PR #7\n"


def test_json_format_is_one_structured_line_for_cloud_logging(capsys, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    say("release", "MERGED PR #7", level="warn", pr=7, item="PAY-101")
    line = capsys.readouterr().out
    entry = json.loads(line)
    assert entry["severity"] == "WARNING" and entry["tag"] == "release"
    assert entry["message"] == "MERGED PR #7" and entry["pr"] == 7
    assert entry["item"] == "PAY-101" and "ts" in entry
    assert line.count("\n") == 1  # exactly one line per entry


def test_no_tagged_print_sneaks_back_into_the_engine():
    """Narration goes through say(); a print("[tag] ...") is the old
    habit. Program OUTPUT (a URL, a report, stderr summaries) is fine."""
    tagged = re.compile(r'print\(\s*f?"\\?n?\[[a-z_-]+\]')
    offenders = [str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "sdlc").rglob("*.py")
                 if p.name != "narrate.py" and tagged.search(p.read_text())]
    assert not offenders, offenders
