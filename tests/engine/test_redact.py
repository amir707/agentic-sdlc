"""orchestrator/redact: no human-facing text may carry a secret."""

from sdlc.engine import redact


def test_url_credentials_are_stripped():
    assert redact.url("https://x-access-token:ghp_abc@github.com/o/r.git") == \
        "https://<redacted>@github.com/o/r.git"
    assert redact.url("fatal: unable to access 'https://u:p@host/x': 403") == \
        "fatal: unable to access 'https://<redacted>@host/x': 403"


def test_env_secret_values_are_blanked():
    assert redact.env_args("CONFIG_TOKEN=abc123,PORT=8080") == \
        "CONFIG_TOKEN=<redacted>,PORT=8080"
    assert redact.env_args("GOOGLE_API_KEY=AQ.x MY_SECRET=y") == \
        "GOOGLE_API_KEY=<redacted> MY_SECRET=<redacted>"


def test_text_applies_both():
    raw = "push https://t:tok@gh/x failed; env API_KEY=k"
    assert "tok" not in redact.text(raw) and "=k" not in redact.text(raw)
