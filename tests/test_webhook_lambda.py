"""Unit tests for src.lambdas.webhook_lambda (API Gateway -> handle_reply).

Everything runs fully offline: the Orchestrator ``handle_reply`` is replaced by
an injected spy, the webhook secret + GitHub token loaders are injected, and no
network / Bedrock / real AWS is touched.

Covers:
- verify_signature: correct sig passes; wrong sig / missing / malformed fail;
  raw-byte digest; constant-time compare rejects a wrong-length sig cleanly.
- base64 body: isBase64Encoded decodes then verifies against the decoded bytes.
- 401 path: invalid/missing signature -> 401 AND handle_reply is NEVER called.
- event translation (spy handle_reply asserts the translated event dict):
  pull_request closed+merged / closed+unmerged / opened(ignored);
  issue_comment created question (OWNER->maintainer) / CONTRIBUTOR->not /
  plain-issue(ignored); pull_request_review submitted; ping pong.
- maintainer identification via author_association + merge-as-ground-truth.
- status-code policy: 200 actionable, 200 ignored, 400 bad JSON, 500 on
  handle_reply failure.
- boundary: the Webhook Lambda never imports dynamo_tools or calls
  transition / write_state.
- end-to-end offline: merged-PR payload + valid signature -> 200 and
  handle_reply called once with the translated event.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
import json
from base64 import b64encode
from pathlib import Path

import pytest

from src.agents.orchestrator import OrchestrationResult

SECRET = "s3cr3t-webhook-key"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class SpyHandleReply:
    """Records every event dict handle_reply is called with."""

    def __init__(self, result=None, raises=False):
        self.calls = []
        self.kwargs = []
        self._result = result
        self._raises = raises

    def __call__(self, event, **kwargs):
        self.calls.append(event)
        self.kwargs.append(kwargs)
        if self._raises:
            raise RuntimeError("simulated handle_reply failure")
        if self._result is not None:
            return self._result
        return OrchestrationResult(
            repo_full_name=event.get("repo_full_name"),
            status="pr_opened",
            stage_reached="reply",
        )


def _sign(body_bytes: bytes, secret: str = SECRET, algo: str = "sha256") -> str:
    """Compute the GitHub-style signature header for a raw body."""
    digestmod = hashlib.sha256 if algo == "sha256" else hashlib.sha1
    digest = hmac.new(secret.encode("utf-8"), body_bytes, digestmod).hexdigest()
    return f"{algo}={digest}"


def _apigw_event(
    *,
    body: str,
    event_type: str,
    signature: str | None = None,
    is_base64: bool = False,
    secret: str = SECRET,
):
    """Build an API Gateway proxy event with a signed body."""
    raw = body.encode("utf-8")
    if signature is None:
        signature = _sign(raw, secret=secret)
    if is_base64:
        transmitted = b64encode(raw).decode("ascii")
    else:
        transmitted = body
    return {
        "headers": {
            "X-GitHub-Event": event_type,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
        "body": transmitted,
        "isBase64Encoded": is_base64,
    }


# ---------------------------------------------------------------------------
# Payload builders (trimmed GitHub webhook shapes)
# ---------------------------------------------------------------------------


def _pull_request_payload(*, repo="owner/repo", action="closed", merged=True, number=42):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
            "title": "Fix the crash",
        },
    }


def _issue_comment_payload(
    *, repo="owner/repo", action="created", body="Why did you change this?",
    association="OWNER", is_pr=True, number=42,
):
    issue = {"number": number}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/pr/42"}
    return {
        "action": action,
        "repository": {"full_name": repo},
        "issue": issue,
        "comment": {"body": body, "author_association": association},
    }


def _review_payload(
    *, repo="owner/repo", action="submitted", body="Could you explain the approach?",
    state="changes_requested", association="MEMBER", number=42,
):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "review": {"body": body, "state": state, "author_association": association},
        "pull_request": {"number": number},
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_faketoken")
    import src.lambdas.webhook_lambda as wl

    importlib.reload(wl)
    wl.reset_clients()
    yield wl
    wl.reset_clients()


def _call(webhook, event, spy):
    """Invoke the handler with the spy and a fixed secret/token loader."""
    return webhook.lambda_handler(
        event,
        None,
        handle_reply_fn=spy,
        secret_loader=lambda: SECRET,
        token_loader=lambda: "ghp_faketoken",
    )


# ---------------------------------------------------------------------------
# verify_signature (REQ-5.1)
# ---------------------------------------------------------------------------


def test_verify_signature_accepts_correct_sha256(webhook):
    body = b'{"hello":"world"}'
    assert webhook.verify_signature(body, _sign(body), SECRET) is True


def test_verify_signature_rejects_wrong_signature(webhook):
    body = b'{"hello":"world"}'
    bad = "sha256=" + "0" * 64
    assert webhook.verify_signature(body, bad, SECRET) is False


def test_verify_signature_rejects_tampered_body(webhook):
    sig = _sign(b'{"a":1}')
    # Same signature, different body -> must fail (digest is over raw bytes).
    assert webhook.verify_signature(b'{"a":2}', sig, SECRET) is False


def test_verify_signature_rejects_missing_and_malformed_header(webhook):
    body = b"payload"
    assert webhook.verify_signature(body, None, SECRET) is False
    assert webhook.verify_signature(body, "", SECRET) is False
    assert webhook.verify_signature(body, "notasignature", SECRET) is False
    assert webhook.verify_signature(body, "sha256=", SECRET) is False


def test_verify_signature_rejects_wrong_length_without_raising(webhook):
    body = b"payload"
    # A short/odd-length hex digest must be rejected cleanly (constant-time
    # compare_digest handles unequal lengths without raising).
    assert webhook.verify_signature(body, "sha256=abcd", SECRET) is False


def test_verify_signature_rejects_unknown_algorithm(webhook):
    body = b"payload"
    assert webhook.verify_signature(body, "md5=deadbeef", SECRET) is False


def test_verify_signature_sha1_fallback(webhook):
    body = b'{"x":1}'
    assert webhook.verify_signature(body, _sign(body, algo="sha1"), SECRET) is True


# ---------------------------------------------------------------------------
# Base64 body
# ---------------------------------------------------------------------------


def test_base64_body_verifies_against_decoded_bytes(webhook):
    spy = SpyHandleReply()
    payload = _pull_request_payload(merged=True)
    body = json.dumps(payload)
    event = _apigw_event(body=body, event_type="pull_request", is_base64=True)
    resp = _call(webhook, event, spy)
    assert resp["statusCode"] == 200
    assert len(spy.calls) == 1
    assert spy.calls[0]["merged"] is True


# ---------------------------------------------------------------------------
# 401 path — before any processing
# ---------------------------------------------------------------------------


def test_invalid_signature_returns_401_and_never_calls_handle_reply(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload())
    event = _apigw_event(body=body, event_type="pull_request", signature="sha256=" + "0" * 64)
    resp = _call(webhook, event, spy)
    assert resp["statusCode"] == 401
    assert spy.calls == []  # before-any-processing rule


def test_missing_signature_returns_401_and_never_calls_handle_reply(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload())
    event = {
        "headers": {"X-GitHub-Event": "pull_request"},
        "body": body,
        "isBase64Encoded": False,
    }
    resp = _call(webhook, event, spy)
    assert resp["statusCode"] == 401
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Event translation — pull_request
# ---------------------------------------------------------------------------


def test_pull_request_closed_merged_translates_to_merged_event(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload(action="closed", merged=True, number=42))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 200
    evt = spy.calls[0]
    assert evt["repo_full_name"] == "owner/repo"
    assert evt["pr_number"] == 42
    assert evt["action"] == "closed"
    assert evt["state"] == "closed"
    assert evt["merged"] is True
    assert "message_type" not in evt


def test_pull_request_closed_unmerged_translates_to_closed_event(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload(action="closed", merged=False))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 200
    evt = spy.calls[0]
    assert evt["merged"] is False
    assert evt["action"] == "closed"


def test_pull_request_opened_is_ignored(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload(action="opened", merged=False))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["message"] == "ignored"
    assert spy.calls == []


def test_pull_request_synchronize_is_ignored(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload(action="synchronize", merged=False))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 200
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Event translation — issue_comment
# ---------------------------------------------------------------------------


def test_issue_comment_question_from_owner_is_maintainer(webhook):
    spy = SpyHandleReply()
    body = json.dumps(
        _issue_comment_payload(body="Why this approach?", association="OWNER")
    )
    resp = _call(webhook, _apigw_event(body=body, event_type="issue_comment"), spy)
    assert resp["statusCode"] == 200
    evt = spy.calls[0]
    assert evt["pr_number"] == 42
    assert evt["text"] == "Why this approach?"
    assert evt["is_maintainer"] is True
    assert "message_type" not in evt


def test_issue_comment_from_contributor_is_not_maintainer(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_issue_comment_payload(association="CONTRIBUTOR"))
    resp = _call(webhook, _apigw_event(body=body, event_type="issue_comment"), spy)
    assert resp["statusCode"] == 200
    assert spy.calls[0]["is_maintainer"] is False


def test_issue_comment_on_plain_issue_is_ignored(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_issue_comment_payload(is_pr=False))
    resp = _call(webhook, _apigw_event(body=body, event_type="issue_comment"), spy)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["message"] == "ignored"
    assert spy.calls == []


def test_issue_comment_member_and_collaborator_are_maintainers(webhook):
    for assoc in ("MEMBER", "COLLABORATOR"):
        spy = SpyHandleReply()
        body = json.dumps(_issue_comment_payload(association=assoc))
        _call(webhook, _apigw_event(body=body, event_type="issue_comment"), spy)
        assert spy.calls[0]["is_maintainer"] is True, assoc


# ---------------------------------------------------------------------------
# Event translation — pull_request_review
# ---------------------------------------------------------------------------


def test_pull_request_review_submitted_translates(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_review_payload(body="Could you clarify?", association="MEMBER"))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request_review"), spy)
    assert resp["statusCode"] == 200
    evt = spy.calls[0]
    assert evt["pr_number"] == 42
    assert evt["text"] == "Could you clarify?"
    assert evt["state"] == "changes_requested"
    assert evt["is_maintainer"] is True


def test_pull_request_review_dismissed_action_is_ignored(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_review_payload(action="dismissed"))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request_review"), spy)
    assert resp["statusCode"] == 200
    assert spy.calls == []


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


def test_ping_returns_pong_without_handle_reply(webhook):
    spy = SpyHandleReply()
    body = json.dumps({"zen": "Keep it logically awesome.", "repository": {"full_name": "owner/repo"}})
    resp = _call(webhook, _apigw_event(body=body, event_type="ping"), spy)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["message"] == "pong"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# is_maintainer_association mapping (REQ-5.1)
# ---------------------------------------------------------------------------


def test_is_maintainer_association_mapping(webhook):
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR", "owner", "member"):
        assert webhook.is_maintainer_association(assoc) is True, assoc
    for assoc in ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", None, "", 5):
        assert webhook.is_maintainer_association(assoc) is False, assoc


# ---------------------------------------------------------------------------
# Status-code policy
# ---------------------------------------------------------------------------


def test_valid_signature_malformed_json_returns_400(webhook):
    spy = SpyHandleReply()
    raw = b"{not valid json"
    sig = _sign(raw)
    event = {
        "headers": {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        "body": raw.decode("utf-8"),
        "isBase64Encoded": False,
    }
    resp = _call(webhook, event, spy)
    assert resp["statusCode"] == 400
    assert spy.calls == []


def test_handle_reply_failure_returns_500_for_github_retry(webhook):
    spy = SpyHandleReply(raises=True)
    body = json.dumps(_pull_request_payload(merged=True))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 500
    assert len(spy.calls) == 1  # it was called, then raised


def test_secret_load_failure_returns_500(webhook):
    def boom():
        raise RuntimeError("secrets manager down")

    body = json.dumps(_pull_request_payload())
    event = _apigw_event(body=body, event_type="pull_request")
    resp = webhook.lambda_handler(
        event, None, handle_reply_fn=SpyHandleReply(), secret_loader=boom
    )
    assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# Delegation threads token / table_name into handle_reply
# ---------------------------------------------------------------------------


def test_handle_reply_receives_token_kwarg(webhook):
    spy = SpyHandleReply()
    body = json.dumps(_pull_request_payload(merged=True))
    webhook.lambda_handler(
        _apigw_event(body=body, event_type="pull_request"),
        None,
        handle_reply_fn=spy,
        secret_loader=lambda: SECRET,
        token_loader=lambda: "ghp_specific",
    )
    assert spy.kwargs[0]["token"] == "ghp_specific"


# ---------------------------------------------------------------------------
# End-to-end offline
# ---------------------------------------------------------------------------


def test_end_to_end_merged_pr_valid_signature(webhook):
    result = OrchestrationResult(
        repo_full_name="owner/repo",
        status="success",
        stage_reached="reply",
        pr_number=42,
    )
    spy = SpyHandleReply(result=result)
    body = json.dumps(_pull_request_payload(action="closed", merged=True, number=42))
    resp = _call(webhook, _apigw_event(body=body, event_type="pull_request"), spy)
    assert resp["statusCode"] == 200
    assert len(spy.calls) == 1
    assert spy.calls[0]["merged"] is True
    assert json.loads(resp["body"])["result"]["status"] == "success"


# ---------------------------------------------------------------------------
# Boundary: the Webhook Lambda is not a state writer (REQ-4.4 / design.md §3)
# ---------------------------------------------------------------------------


def test_webhook_does_not_import_dynamo_tools_or_write_state(webhook):
    source = Path(webhook.__file__).read_text()
    tree = ast.parse(source)

    # No import of dynamo_tools.
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
    assert not any("dynamo_tools" in name for name in imported)

    # No transition / write_state calls anywhere.
    forbidden = {"transition", "write_state"}
    offenders = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert offenders == []
