"""Dashboard Lambda — read-only DynamoDB scan → JSON for the static dashboard.

Triggered by **API Gateway (GET /state)** (design.md section 9). Its single job
is "scan ``ResurrectorState`` → JSON for dashboard": it reads every repo record
and returns a JSON-safe snapshot the static ``src/dashboard/index.html`` page
fetches and renders (REQ-7.2, REQ-7.3).

Read-only boundary (design.md section 3, aws-constraints.md ``resurrector-dashboard``)
-------------------------------------------------------------------------------------
The Orchestrator is the *only* component that writes DynamoDB state. This Lambda
is the tightest-scoped one in the system: it performs a ``dynamodb:Scan`` and
nothing else. It never calls ``transition`` / ``write_state`` / ``put_item`` /
``update_item`` and does not import the write helpers from
:mod:`src.tools.dynamo_tools` (an AST test enforces this). It also does not
import strands — it is a plain data endpoint, not an agent.

Freshness (REQ-7.2)
-------------------
The scan reads live DynamoDB on every request, and the static page auto-refreshes
every 30 seconds (see ``index.html``). A refresh therefore reflects any
transition within 30 seconds of it landing, comfortably inside REQ-7.2's 60-second
budget. Every transition stamps ``last_action_at`` (REQ-7.1, enforced in
:mod:`src.tools.dynamo_tools`), so the dashboard can also show per-repo freshness.

Response shape (REQ-7.3)
------------------------
``lambda_handler`` returns an API Gateway proxy response whose body is::

    {"generated_at": "<iso>", "count": N, "repos": [ {row}, ... ]}

Each row carries at least the REQ-7.3 fields — ``repo_full_name``, ``status``,
``pr_url``, ``last_action_at``, ``maintainer_responded`` — plus the rest of the
record (``issue_number``, ``pr_number``, ``opened_at``, ``follow_up_count``,
``complexity``, ``notes``). Rows are sorted by ``last_action_at`` **descending**
(newest activity first); rows with a missing/empty ``last_action_at`` sort last.

Decimal handling
----------------
Numeric attributes come back from the boto3 DynamoDB *resource* as ``Decimal``,
which ``json.dumps`` cannot serialize. We parse each raw item through
:meth:`src.tools.dynamo_tools.RepoState.from_item` (which already coerces the
key numerics to ``int``) and emit ``RepoState.to_item()`` — a plain dict of
JSON-safe values. Reusing ``RepoState`` keeps one definition of the row shape and
its coercions rather than hand-rolling a Decimal-aware encoder.

CORS + security note
--------------------
The static page is served from S3/CloudFront and fetches this API cross-origin,
so responses carry ``Access-Control-Allow-Origin: *``. The endpoint is
**unauthenticated** and therefore exposes repo-engagement state publicly. That is
acceptable here because the table stores only public-repo metadata and no PII
(design.md section 11, "No PII stored"). To tighten, pin the header to the
CloudFront domain and/or front the API with an authorizer — noted for a future
hardening pass; the payload contains nothing sensitive today.

Configuration
-------------
- ``RESURRECTOR_STATE_TABLE`` — DynamoDB table name (default ``ResurrectorState``,
  resolved by :mod:`src.tools.dynamo_tools`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

from src.tools import dynamo_tools
from src.tools.dynamo_tools import RepoState

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:  # pragma: no cover - Lambda configures the root logger
    LOGGER.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Lazily-created AWS resource (built on first use so moto / tests can intercept).
# ---------------------------------------------------------------------------

_DDB_RESOURCE: Any = None


def _ddb_resource() -> Any:
    """Return the process-wide DynamoDB resource, creating it on first use."""
    global _DDB_RESOURCE
    if _DDB_RESOURCE is None:
        _DDB_RESOURCE = boto3.resource("dynamodb")
    return _DDB_RESOURCE


def reset_clients() -> None:
    """Drop the cached DynamoDB resource (used by tests)."""
    global _DDB_RESOURCE
    _DDB_RESOURCE = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Core scan (read-only)
# ---------------------------------------------------------------------------


def _sort_key(row: dict[str, Any]) -> str:
    """Sort key placing the newest ``last_action_at`` first.

    Missing/empty timestamps sort last (empty string is smallest, so with a
    descending sort they land at the bottom).
    """
    value = row.get("last_action_at")
    return value if isinstance(value, str) else ""


def get_dashboard_data(
    *,
    table_name: Optional[str] = None,
    ddb_resource: Any = None,
) -> list[dict[str, Any]]:
    """Scan ``ResurrectorState`` and return JSON-safe rows, newest first.

    Handles DynamoDB Scan pagination by following ``LastEvaluatedKey`` until the
    table is exhausted, so no rows are dropped on a large table. Each raw item is
    parsed through :meth:`RepoState.from_item` (coercing ``Decimal`` numerics to
    ``int``) and re-emitted via :meth:`RepoState.to_item`, guaranteeing the
    result is safe for ``json.dumps``. Rows are sorted by ``last_action_at``
    descending.

    ``table_name`` and ``ddb_resource`` are injectable so the whole flow runs
    offline against moto or a fake resource with no network.
    """
    resource = ddb_resource if ddb_resource is not None else _ddb_resource()
    name = table_name or dynamo_tools._table_name()
    table = resource.Table(name)

    rows: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            rows.append(RepoState.from_item(item).to_item())
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    rows.sort(key=_sort_key, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# API Gateway entry point (GET /state)
# ---------------------------------------------------------------------------


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response with a JSON body + CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # Permissive CORS: the static S3/CloudFront page fetches this
            # cross-origin. Payload is public-repo metadata only (design §11).
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(payload),
    }


def lambda_handler(
    event: Any,
    context: Any = None,  # noqa: ARG001
    *,
    table_name: Optional[str] = None,
    ddb_resource: Any = None,
) -> dict[str, Any]:
    """API Gateway ``GET /state`` entry point (REQ-7.2, REQ-7.3).

    Scans the state table and returns
    ``{"generated_at", "count", "repos": [...]}`` as JSON. An empty table yields
    ``repos: []`` with a 200. A scan failure is logged and returned as a 500 with
    a JSON error body — the handler never crashes.

    ``event`` is ignored apart from optional injection seams; ``table_name`` and
    ``ddb_resource`` are injectable so tests need no network.
    """
    try:
        repos = get_dashboard_data(table_name=table_name, ddb_resource=ddb_resource)
    except Exception:  # noqa: BLE001 - a scan failure must not crash the endpoint
        LOGGER.exception("dashboard scan failed")
        return _response(500, {"error": "failed to load dashboard state"})

    return _response(
        200,
        {
            "generated_at": _now_iso(),
            "count": len(repos),
            "repos": repos,
        },
    )
