"""Secret redaction for anything human-facing (logs, errors, echoed
commands, tracebacks). Two shapes of secret travel through this system
by design — never in config, always transient — and no path may echo
them raw:

- credentials embedded in git URLs (clone/push carry the PAT):
  https://x-access-token:ghp_...@github.com/... -> //<redacted>@
- KEY=value pairs in command arguments (gcloud --set-env-vars carries
  CONFIG_TOKEN=..., ...KEY=..., ...SECRET=...):  TOKEN=abc -> TOKEN=<redacted>

`text()` applies both; use the specific one when the input's shape is
known (cheaper, and it documents intent at the call site).
"""

import re

_URL_CREDS = re.compile(r"//[^@/]+@")
_ENV_SECRETS = re.compile(r"((?:TOKEN|KEY|SECRET)[A-Z_]*=)[^,\s]+")


def url(text: str) -> str:
    """Strip embedded credentials from a git/HTTP URL (or any text
    containing one)."""
    return _URL_CREDS.sub("//<redacted>@", text)


def env_args(text: str) -> str:
    """Blank the value of every *TOKEN= / *KEY= / *SECRET= pair."""
    return _ENV_SECRETS.sub(r"\1<redacted>", text)


def text(value: str) -> str:
    """Both, for text of unknown provenance (a caught error's message)."""
    return env_args(url(value))
