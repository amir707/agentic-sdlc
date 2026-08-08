"""auth_headers: the one place store-request auth is assembled.

Locally the role token rides X-Store-Token and nothing else happens.
With STORE_IAM_AUTH set (the cloud deployment sets it), a Google
identity token for the store's ORIGIN is fetched per request into
Authorization — per request because identity tokens expire hourly and
the resident services run indefinitely.
"""

from unittest import mock

from adapters.store_client import auth_headers

URL = "https://delivery-store-xyz.a.run.app/mcp"


def test_local_default_is_x_store_token_only(monkeypatch):
    monkeypatch.delenv("STORE_IAM_AUTH", raising=False)
    headers = auth_headers("role-token", URL)
    assert headers == {"X-Store-Token": "role-token"}


def test_iam_mode_attaches_identity_token_for_the_origin(monkeypatch):
    monkeypatch.setenv("STORE_IAM_AUTH", "1")
    with mock.patch("google.oauth2.id_token.fetch_id_token",
                    return_value="google-signed") as fetch:
        headers = auth_headers("role-token", URL)
    assert headers["X-Store-Token"] == "role-token"
    assert headers["Authorization"] == "Bearer google-signed"
    # audience is the service origin, not the /mcp path
    assert fetch.call_args.args[1] == "https://delivery-store-xyz.a.run.app"
