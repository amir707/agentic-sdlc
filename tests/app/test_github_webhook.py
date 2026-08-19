"""sdlc.app.webhook: GitHub deliveries become the same nudge the
heartbeat produces — signed, filtered, fire-and-forget."""

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sdlc.app import webhook

SECRET = "s3cret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- the pure rules ----------------------------------------------------------

def test_gate_command_comment_on_a_pr_is_dispatched():
    payload = {"action": "created", "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/7"}},
               "comment": {"body": "/approve looks good"}}
    assert webhook.classify("issue_comment", payload) == "github:comment:7"


def test_chatter_and_issue_comments_are_ignored():
    chatter = {"action": "created", "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/7"}},
               "comment": {"body": "nice work"}}
    assert webhook.classify("issue_comment", chatter) is None
    plain_issue = {"action": "created", "issue": {"number": 3},
                   "comment": {"body": "/approve"}}
    assert webhook.classify("issue_comment", plain_issue) is None
    edited = {"action": "edited", "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/7"}},
              "comment": {"body": "/approve"}}
    assert webhook.classify("issue_comment", edited) is None


def test_new_pr_head_is_dispatched_other_pr_actions_are_not():
    assert webhook.classify("pull_request", {"action": "synchronize", "number": 7}) == "github:pr_synchronize:7"
    assert webhook.classify("pull_request", {"action": "reopened", "number": 7}) == "github:pr_reopened:7"
    assert webhook.classify("pull_request", {"action": "labeled", "number": 7}) is None
    assert webhook.classify("push", {}) is None


def test_signature_verification():
    body = b'{"x": 1}'
    assert webhook.verify_signature(SECRET, body, _sig(body))
    assert not webhook.verify_signature(SECRET, body, _sig(body, "other"))
    assert not webhook.verify_signature(SECRET, body, None)
    assert not webhook.verify_signature(SECRET, body, "sha1=abc")


# --- the route ------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    sent = []

    async def fake_post_event(url, name):
        sent.append((url, name))
    monkeypatch.setattr(webhook, "post_event", fake_post_event)
    app = FastAPI()
    webhook.mount_github_webhook(
        app, secret=SECRET,
        targets=["http://127.0.0.1:8789/apps/sprint/trigger/pubsub",
                 "http://127.0.0.1:8788/apps/release/trigger/pubsub"])
    return TestClient(app), sent


def _post(tc, event, payload, sig=None):
    body = json.dumps(payload).encode()
    return tc.post(webhook.WEBHOOK_PATH, content=body, headers={
        "X-GitHub-Event": event, "Content-Type": "application/json",
        "X-Hub-Signature-256": _sig(body) if sig is None else sig})


def test_bad_signature_is_401_and_nothing_is_dispatched(client):
    tc, sent = client
    r = _post(tc, "issue_comment", {"action": "created"}, sig="sha256=00")
    assert r.status_code == 401 and sent == []


def test_ping_pongs_without_dispatch(client):
    tc, sent = client
    assert _post(tc, "ping", {"zen": "x"}).json() == {"pong": True} and sent == []


def test_gate_comment_nudges_every_target_in_the_background(client):
    tc, sent = client
    payload = {"action": "created", "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/7"}},
               "comment": {"body": "/approve"}}
    with tc:  # run the app's event loop so background tasks complete
        r = _post(tc, "issue_comment", payload)
        assert r.status_code == 202 and r.json()["dispatched"] == "github:comment:7"
    assert {u for u, _ in sent} == {"http://127.0.0.1:8789/apps/sprint/trigger/pubsub",
                                    "http://127.0.0.1:8788/apps/release/trigger/pubsub"}
    assert all(n == "github:comment:7" for _, n in sent)


def test_irrelevant_event_is_acknowledged_not_dispatched(client):
    tc, sent = client
    r = _post(tc, "pull_request", {"action": "labeled", "number": 7})
    assert r.status_code == 202 and r.json() == {"ignored": "pull_request"} and sent == []
