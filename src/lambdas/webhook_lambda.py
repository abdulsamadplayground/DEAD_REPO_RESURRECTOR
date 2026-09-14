"""Webhook Lambda — verify, identify the maintainer, translate, and delegate.

Triggered by **API Gateway (POST /webhook)** (design.md section 9). GitHub calls
this endpoint whenever something happens on a repo we have an open PR against.
Its job is the three things the Orchestrator explicitly does *not* do
(orchestrator.py "REQ-5.1 boundary"): **verify the webhook signature**,
**identify whether the actor is the maintainer**, and **translate the GitHub
payload into the event dict** that :func:`src.agents.orchestrator.handle_reply`
consumes. The state transitions themselves (REQ-5.2 merged→success,
REQ-5.3 question→reply, REQ-5.4 closed→rejected) are performed by
``handle_reply``, which this Lambda calls with an already-verified event. This
Lambda therefore never writes DynamoDB state and never classifies the reply — it
only verifies, identifies, translates, and delegates.

HMAC verification (REQ-5.1, design.md section 11) — "before any processing"
---------------------------------------------------------------------------
GitHub signs the **raw request body** with the shared secret using HMAC-SHA256
and sends it in the ``X-Hub-Signature-256`` header as ``sha256=<hexdigest>``
(the older ``X-Hub-Signature`` sha1 header is accepted only as a fallback).
:func:`verify_signature` recomputes the digest over the *exact* bytes received
and compares with :func:`hmac.compare_digest` (constant-time). Two rules matter:

1. Verify against the **raw body bytes as received**, before any JSON parse or
   re-serialization — re-serializing changes bytes and breaks the digest. API
   Gateway may base64-encode the body (``event["isBase64Encoded"]``); we decode
   first and verify against those decoded bytes.
2. A missing or invalid signature returns **HTTP 401 and the body is never
   parsed or processed** — ``handle_reply`` is not reached. This is the
   "verification before any processing" rule (design.md section 11), covered by
   a test that a bad signature never reaches the injected ``handle_reply``.

Maintainer identification (REQ-5.1)
-----------------------------------
"Maintainer" = the actor has write/admin authority over the repo. Signals used,
in order:

- A **merge is ground truth**: ``pull_request.merged == true`` means the merger
  had merge rights, so ``is_maintainer`` is ``True`` for a merge regardless of
  anything else (and ``handle_reply`` acts on a merge/close independently of the
  flag anyway).
- For comments/reviews, GitHub's ``author_association`` field:
  ``OWNER`` / ``MEMBER`` / ``COLLABORATOR`` indicate write access →
  ``is_maintainer=True``; ``CONTRIBUTOR`` / ``NONE`` / ``FIRST_TIME_CONTRIBUTOR``
  → ``False``. Non-maintainer comments are still passed through (``handle_reply``
  records them as a no-op), but with ``is_maintainer=False`` so it does not
  falsely mark ``maintainer_responded``.

Event translation (design.md section 6: pull_request, issue_comment,
pull_request_review)
--------------------------------------------------------------------
The ``X-GitHub-Event`` header names the event type; the JSON body carries the
payload. :func:`translate_event` maps each to the ``handle_reply`` event dict, or
returns ``None`` for events we intentionally ignore (a 200 "ignored" response —
we never call ``handle_reply`` for an irrelevant action, to avoid needless
work). The translated dict never carries ``message_type`` — that key routes to
``handle_reply``'s follow-up-timer branch, which only the Processor feeds.
``ping`` (sent on webhook registration) is answered with a 200 pong and never
reaches ``handle_reply``.

Status-code policy (and the GitHub-retry reasoning)
---------------------------------------------------
- **401** — missing/invalid signature. Deterministic; a retry cannot help, and
  we must not process an unverified body.
- **400** — valid signature but a body that is not parseable JSON. Also
  deterministic; a retry cannot help.
- **200** — verified and either delegated successfully *or* intentionally
  ignored (ping, irrelevant action, comment on a plain issue). Returning 200 for
  "valid but not actionable" stops GitHub from retry-storming.
- **500** — a ``handle_reply`` failure or a genuinely unexpected error. These may
  be *transient* (DynamoDB/GitHub/Bedrock hiccup), so a 500 lets GitHub retry;
  the error is logged. We deliberately do **not** swallow ``handle_reply``
  failures as 200, because that would drop a real maintainer event.

Boundary (design.md section 3)
------------------------------
This Lambda is not a state writer: it does not import
:mod:`src.tools.dynamo_tools` and never calls ``transition`` / ``write_state``.
Every state change happens inside the injected ``handle_reply``. An AST test
enforces the no-import rule and a spy test enforces the delegation.

Configuration (env vars; ``_env`` helpers per house style)
----------------------------------------------------------
- ``RESURRECTOR_WEBHOOK_SECRET_ID`` — Secrets Manager id for the HMAC shared
  secret (default ``/resurrector/webhook-secret``).
- ``RESURRECTOR_GITHUB_TOKEN_SECRET_ID`` — Secrets Manager id for the GitHub PAT
  used by the reply path (default ``/resurrector/github-token``); loaded via
  :func:`src.lambdas.scanner_lambda.load_github_token` so there is one token
  loader in the codebase.
- ``RESURRECTOR_WEBHOOK_SECRET`` — local-testing fallback checked before Secrets
  Manager (mirrors the Scanner's ``GITHUB_TOKEN`` fallback).

Verification status
-------------------
Everything below is exercised offline with an injected ``handle_reply``, an
injected secret loader, and no network. The live pieces no offline test can
cover — real API Gateway invocation, the Bedrock round-trip inside the reply
path, and real GitHub/DynamoDB calls — remain unverified without credentials
(Task 13).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from base64 import b64decode
from typing import Any, Callable, Optional

import boto3

from src.agents import orchestrator
from src.lambdas.scanner_lambda import load_github_token

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:  # pragma: no cover - Lambda configures the root logger
    LOGGER.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WEBHOOK_SECRET_ID_ENV_VAR = "RESURRECTOR_WEBHOOK_SECRET_ID"
WEBHOOK_SECRET_ENV_VAR = "RESURRECTOR_WEBHOOK_SECRET"

DEFAULT_WEBHOOK_SECRET_ID = "/resurrector/webhook-secret"

#: Header GitHub sends the HMAC-SHA256 signature in (design.md section 6).
SIGNATURE_256_HEADER = "x-hub-signature-256"
#: Legacy HMAC-SHA1 header, accepted only as a fallback.
SIGNATURE_1_HEADER = "x-hub-signature"
#: Header naming the GitHub event type.
EVENT_HEADER = "x-github-event"

#: ``author_association`` values that indicate write/admin access (REQ-5.1).
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class ConfigurationError(RuntimeError):
    """Raised when required Lambda configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Lazily-created AWS clients (built on first use so tests / moto can intercept).
# ---------------------------------------------------------------------------

_SECRETS_CLIENT: Any = None
_WEBHOOK_SECRET_CACHE: Optional[str] = None


def _secrets_client() -> Any:
    """Return the process-wide Secrets Manager client, created on first use."""
    global _SECRETS_CLIENT
    if _SECRETS_CLIENT is None:
        _SECRETS_CLIENT = boto3.client("secretsmanager")
    return _SECRETS_CLIENT


def reset_clients() -> None:
    """Drop cached AWS clients and the cached secret (used by tests)."""
    global _SECRETS_CLIENT, _WEBHOOK_SECRET_CACHE
    _SECRETS_CLIENT = None
    _WEBHOOK_SECRET_CACHE = None


def load_webhook_secret(
    *,
    secret_id: Optional[str] = None,
    secrets_client: Any = None,
    use_cache: bool = True,
) -> str:
    """Load the webhook HMAC secret, caching it for the life of the container.

    Resolution order mirrors :func:`src.lambdas.scanner_lambda.load_github_token`
    (design.md sections 6 and 11): the ``RESURRECTOR_WEBHOOK_SECRET`` env var
    (local testing / SAM local) then Secrets Manager at
    ``RESURRECTOR_WEBHOOK_SECRET_ID`` (default ``/resurrector/webhook-secret``).
    The stored value may be a raw string or a JSON object with a ``secret`` /
    ``webhook_secret`` key. ``secrets_client`` is injectable so tests never need
    AWS.
    """
    global _WEBHOOK_SECRET_CACHE
    if use_cache and _WEBHOOK_SECRET_CACHE:
        return _WEBHOOK_SECRET_CACHE

    env_secret = os.environ.get(WEBHOOK_SECRET_ENV_VAR)
    if env_secret:
        if use_cache:
            _WEBHOOK_SECRET_CACHE = env_secret
        return env_secret

    resolved_id = secret_id or os.environ.get(
        WEBHOOK_SECRET_ID_ENV_VAR, DEFAULT_WEBHOOK_SECRET_ID
    )
    client = secrets_client if secrets_client is not None else _secrets_client()
    response = client.get_secret_value(SecretId=resolved_id)
    raw = response.get("SecretString")
    if not raw:
        raise ConfigurationError(f"secret {resolved_id!r} has no SecretString value")

    secret = raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        secret = parsed.get("secret") or parsed.get("webhook_secret") or ""
    if not secret:
        raise ConfigurationError(f"secret {resolved_id!r} does not contain a secret")

    if use_cache:
        _WEBHOOK_SECRET_CACHE = secret
    return secret


# ---------------------------------------------------------------------------
# HMAC verification (REQ-5.1, design.md section 11)
# ---------------------------------------------------------------------------


def verify_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Return ``True`` iff ``signature_header`` matches the HMAC of ``raw_body``.

    ``signature_header`` is GitHub's ``X-Hub-Signature-256`` value, of the form
    ``sha256=<hexdigest>`` (or ``sha1=<hexdigest>`` for the legacy fallback). The
    digest is computed over the **raw body bytes exactly as received** and
    compared with :func:`hmac.compare_digest` for constant-time equality. A
    missing, malformed, or unknown-algorithm header returns ``False`` rather than
    raising, so a bad signature is a clean 401 rather than a 500.
    """
    if not signature_header or not isinstance(signature_header, str):
        return False
    if "=" not in signature_header:
        return False

    algo, _, sent_digest = signature_header.partition("=")
    algo = algo.strip().lower()
    sent_digest = sent_digest.strip()
    if not sent_digest:
        return False

    if algo == "sha256":
        digestmod = hashlib.sha256
    elif algo == "sha1":  # legacy fallback only
        digestmod = hashlib.sha1
    else:
        return False

    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = hmac.new(secret_bytes, raw_body, digestmod).hexdigest()
    # compare_digest is constant-time and safe against differing lengths.
    return hmac.compare_digest(expected, sent_digest)


# ---------------------------------------------------------------------------
# API Gateway request helpers
# ---------------------------------------------------------------------------


def _normalize_headers(headers: Any) -> dict[str, str]:
    """Lower-case every header name so lookups are case-insensitive.

    API Gateway (REST vs HTTP API) is inconsistent about header casing; GitHub
    itself sends ``X-Hub-Signature-256`` but proxies may fold the case. A
    ``None`` header map (some HTTP-API payloads) becomes an empty dict.
    """
    if not isinstance(headers, dict):
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


def _raw_body_bytes(event: dict[str, Any]) -> bytes:
    """Return the request body as raw bytes, decoding base64 when flagged.

    The HMAC digest must be computed over these exact bytes (see the module
    docstring). When ``event["isBase64Encoded"]`` is true the body is base64;
    we decode it first and verify/parse against the decoded bytes.
    """
    body = event.get("body")
    if body is None:
        return b""
    if event.get("isBase64Encoded"):
        if isinstance(body, str):
            return b64decode(body)
        return b64decode(bytes(body))
    if isinstance(body, bytes):
        return body
    return body.encode("utf-8")


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response with a JSON body."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


# ---------------------------------------------------------------------------
# Maintainer identification + event translation (REQ-5.1, design.md section 6)
# ---------------------------------------------------------------------------


def is_maintainer_association(association: Any) -> bool:
    """Map a GitHub ``author_association`` to a maintainer (write-access) bool.

    ``OWNER`` / ``MEMBER`` / ``COLLABORATOR`` → ``True``; everything else
    (``CONTRIBUTOR`` / ``NONE`` / ``FIRST_TIME_CONTRIBUTOR`` / missing) →
    ``False`` (REQ-5.1).
    """
    if not isinstance(association, str):
        return False
    return association.strip().upper() in MAINTAINER_ASSOCIATIONS


def translate_event(
    github_event: Optional[str], payload: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Translate a GitHub webhook payload into a ``handle_reply`` event dict.

    Returns the maintainer-activity event dict for an actionable event, or
    ``None`` for an event we intentionally ignore (the handler answers those with
    a 200 "ignored"). The returned dict never carries ``message_type`` so it
    always routes to ``handle_reply``'s maintainer-activity branch, never the
    follow-up-timer branch.

    Handled events (design.md section 6):

    - ``pull_request`` — only the ``closed`` action is actionable: ``merged=true``
      → merged (REQ-5.2), ``merged=false`` → closed/rejected (REQ-5.4). Every
      other action (opened / synchronize / reopened / ...) is ignored.
    - ``issue_comment`` — only ``created`` comments *on a PR* (``issue.pull_request``
      present); ``comment.body`` → ``text`` (REQ-5.3), ``author_association`` →
      ``is_maintainer``. A comment on a plain issue is ignored.
    - ``pull_request_review`` — only ``submitted`` reviews; ``review.body`` →
      ``text``, ``review.state`` → ``state`` (never ``"closed"``, so it never
      trips the closed branch), ``author_association`` → ``is_maintainer``.
    """
    repo_full_name = (payload.get("repository") or {}).get("full_name")
    if not repo_full_name:
        return None

    if github_event == "pull_request":
        if payload.get("action") != "closed":
            return None
        pr = payload.get("pull_request") or {}
        merged = bool(pr.get("merged"))
        return {
            "repo_full_name": repo_full_name,
            "pr_number": pr.get("number"),
            "action": "closed",
            "state": pr.get("state") or "closed",
            "merged": merged,
            # A merge/close required write access — treat the actor as the
            # maintainer (handle_reply acts on merge/close regardless anyway).
            "is_maintainer": True,
        }

    if github_event == "issue_comment":
        if payload.get("action") != "created":
            return None
        issue = payload.get("issue") or {}
        # Only comments on a PR are relevant; a plain-issue comment is ignored.
        if not issue.get("pull_request"):
            return None
        comment = payload.get("comment") or {}
        return {
            "repo_full_name": repo_full_name,
            "pr_number": issue.get("number"),
            "action": "created",
            "text": comment.get("body"),
            "is_maintainer": is_maintainer_association(
                comment.get("author_association")
            ),
        }

    if github_event == "pull_request_review":
        if payload.get("action") != "submitted":
            return None
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        return {
            "repo_full_name": repo_full_name,
            "pr_number": pr.get("number"),
            "action": "submitted",
            "text": review.get("body"),
            "state": review.get("state"),
            "is_maintainer": is_maintainer_association(
                review.get("author_association")
            ),
        }

    # Any other event type (push, fork, star, ...) is not actionable.
    return None


# ---------------------------------------------------------------------------
# Lambda entry point (API Gateway proxy integration)
# ---------------------------------------------------------------------------


def lambda_handler(
    event: dict[str, Any],
    context: Any = None,  # noqa: ARG001
    *,
    handle_reply_fn: Optional[Callable[..., Any]] = None,
    secret_loader: Optional[Callable[[], str]] = None,
    token_loader: Optional[Callable[[], Optional[str]]] = None,
    table_name: Optional[str] = None,
) -> dict[str, Any]:
    """API Gateway ``POST /webhook`` entry point (REQ-5.1–5.4).

    The flow, in order (see the module docstring for the full rationale):

    1. Extract the raw body bytes (base64-decoding when flagged) and the
       case-insensitive headers.
    2. **Verify the HMAC signature over the raw bytes first.** Missing/invalid →
       ``401`` and the body is never parsed (before-any-processing rule).
    3. Answer ``ping`` with a ``200`` pong (no ``handle_reply``).
    4. Parse the JSON body; unparseable → ``400``.
    5. Translate the payload; an ignored event → ``200`` "ignored".
    6. Delegate the translated event to ``handle_reply`` (which performs the
       state transition and, for a question, the reply). Success → ``200``; a
       ``handle_reply`` failure → ``500`` so GitHub retries.

    Every seam is injectable: ``handle_reply_fn`` (defaults to
    :func:`src.agents.orchestrator.handle_reply`), ``secret_loader`` (defaults to
    :func:`load_webhook_secret`), and ``token_loader`` (defaults to
    :func:`src.lambdas.scanner_lambda.load_github_token`). No network in tests.
    """
    _handle_reply = handle_reply_fn or orchestrator.handle_reply
    _load_secret = secret_loader or load_webhook_secret
    _load_token = token_loader or load_github_token

    headers = _normalize_headers(event.get("headers"))
    raw_body = _raw_body_bytes(event)

    # -- 2. HMAC verification BEFORE any processing (REQ-5.1) -------------
    signature = headers.get(SIGNATURE_256_HEADER) or headers.get(SIGNATURE_1_HEADER)
    try:
        secret = _load_secret()
    except Exception:  # noqa: BLE001 - a secret-load failure is a server error
        LOGGER.exception("failed to load webhook secret")
        return _response(500, {"error": "secret unavailable"})

    if not verify_signature(raw_body, signature, secret):
        LOGGER.warning("rejecting webhook: missing or invalid signature")
        return _response(401, {"error": "invalid signature"})

    github_event = headers.get(EVENT_HEADER)

    # -- 3. ping handshake (sent on webhook registration) ----------------
    if github_event == "ping":
        return _response(200, {"message": "pong"})

    # -- 4. parse the (now-verified) body --------------------------------
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except (ValueError, UnicodeDecodeError):
        LOGGER.warning("verified webhook has an unparseable body")
        return _response(400, {"error": "malformed JSON body"})

    # -- 5. translate; ignore non-actionable events ----------------------
    reply_event = translate_event(github_event, payload)
    if reply_event is None:
        LOGGER.info(
            "ignoring webhook event=%s action=%s (not actionable)",
            github_event,
            payload.get("action"),
        )
        return _response(200, {"message": "ignored", "event": github_event})

    # -- 6. delegate to handle_reply (it owns every state transition) ----
    try:
        token = _load_token()
    except Exception:  # noqa: BLE001 - token is only needed for a question reply
        LOGGER.warning("could not load GitHub token; proceeding without it")
        token = None

    try:
        result = _handle_reply(reply_event, token=token, table_name=table_name)
    except Exception:  # noqa: BLE001 - a transient failure should let GitHub retry
        LOGGER.exception(
            "handle_reply failed for %s", reply_event.get("repo_full_name")
        )
        return _response(500, {"error": "handle_reply failed"})

    body: dict[str, Any] = {"message": "processed", "event": github_event}
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        body["result"] = to_dict()
    return _response(200, body)
