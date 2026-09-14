"""Typed DynamoDB helpers for the ResurrectorState table.

The Orchestrator is the only component that writes DynamoDB state (see design.md
section 3). This module provides a small typed surface over the single-table
``ResurrectorState`` design:

- :class:`RepoState` — a dataclass mirroring the table's attributes.
- :func:`read_state` — read one repo's record.
- :func:`write_state` — persist a full record.
- :func:`transition` — apply a status change (and arbitrary attribute updates)
  while always refreshing ``last_action_at`` to now (ISO-8601). REQ-7.1 requires
  ``last_action_at`` to be updated on every transition.

The table name is configurable via the ``RESURRECTOR_STATE_TABLE`` environment
variable and defaults to ``ResurrectorState``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TABLE_NAME = "ResurrectorState"
TABLE_NAME_ENV_VAR = "RESURRECTOR_STATE_TABLE"

# Valid status values per design.md section 4.
VALID_STATUSES = frozenset(
    {
        "discovered",
        "in_progress",
        "skipped_complex",
        "fix_failed",
        "pr_opened",
        "success",
        "rejected",
        "dormant",
        "ignored",
    }
)


def _table_name() -> str:
    """Resolve the configured table name (env var wins, else default)."""
    return os.environ.get(TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _get_table(table_name: Optional[str] = None):
    """Return a boto3 DynamoDB Table resource for the state table."""
    resource = boto3.resource("dynamodb")
    return resource.Table(table_name or _table_name())


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RepoState:
    """One row in the ``ResurrectorState`` table.

    ``repo_full_name`` is the partition key (``owner/repo``). All other fields
    are optional so a freshly-discovered repo can be represented with minimal
    data. Optional attributes that are ``None`` are omitted when persisted so we
    never write empty/typeless values to DynamoDB.
    """

    repo_full_name: str
    status: str = "discovered"
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    opened_at: Optional[str] = None
    last_action_at: Optional[str] = None
    maintainer_responded: bool = False
    follow_up_count: int = 0
    complexity: Optional[str] = None
    notes: Optional[str] = None

    def to_item(self) -> dict[str, Any]:
        """Serialize to a DynamoDB item, dropping ``None`` values."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "RepoState":
        """Build a :class:`RepoState` from a raw DynamoDB item.

        Numeric attributes come back from boto3 as ``Decimal``; coerce those to
        ``int``. Unknown keys are ignored so the model stays forward-compatible.
        """
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in item.items():
            if key not in known:
                continue
            if key in ("issue_number", "pr_number", "follow_up_count") and value is not None:
                kwargs[key] = int(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read_state(
    repo_full_name: str, *, table_name: Optional[str] = None
) -> Optional[RepoState]:
    """Read the record for ``repo_full_name``.

    Returns ``None`` when no record exists. Used by the Scanner dedup check
    (REQ-1.3) to determine a repo's current status before enqueueing.
    """
    table = _get_table(table_name)
    response = table.get_item(Key={"repo_full_name": repo_full_name})
    item = response.get("Item")
    if item is None:
        return None
    return RepoState.from_item(item)


def write_state(state: RepoState, *, table_name: Optional[str] = None) -> RepoState:
    """Persist ``state`` to DynamoDB and return it.

    Always stamps ``last_action_at`` to now when it is unset so callers that
    build a fresh :class:`RepoState` still satisfy REQ-7.1.
    """
    if state.last_action_at is None:
        state.last_action_at = _now_iso()
    table = _get_table(table_name)
    table.put_item(Item=state.to_item())
    return state


def transition(
    repo_full_name: str,
    new_status: str,
    *,
    table_name: Optional[str] = None,
    **updates: Any,
) -> RepoState:
    """Apply a status change and refresh ``last_action_at`` atomically.

    Performs a single ``update_item`` upsert — no read is done first. The call
    sets ``new_status`` plus any keyword ``updates`` (e.g. ``pr_number``,
    ``pr_url``, ``issue_number``, ``opened_at`` for REQ-4.4) and stamps
    ``last_action_at`` to now (REQ-7.1). If no record exists for
    ``repo_full_name``, DynamoDB creates one containing just the key and the
    attributes supplied here; attributes not named in the call are left
    untouched (existing record) or absent (new record), which means dataclass
    defaults are NOT applied on insert.

    Returns the post-update record (``ReturnValues="ALL_NEW"``) as a
    :class:`RepoState`.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {new_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )

    unknown = set(updates) - {f.name for f in fields(RepoState)}
    if unknown:
        raise ValueError(f"unknown attribute(s): {sorted(unknown)}")

    now = _now_iso()
    attributes: dict[str, Any] = {
        "status": new_status,
        "last_action_at": now,
        **updates,
    }
    # repo_full_name is the key; never set it as a mutable attribute.
    attributes.pop("repo_full_name", None)

    table = _get_table(table_name)

    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {}
    set_clauses: list[str] = []
    for i, (key, value) in enumerate(attributes.items()):
        name_ph = f"#a{i}"
        value_ph = f":v{i}"
        expr_names[name_ph] = key
        expr_values[value_ph] = value
        set_clauses.append(f"{name_ph} = {value_ph}")

    response = table.update_item(
        Key={"repo_full_name": repo_full_name},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return RepoState.from_item(response["Attributes"])


def increment_follow_up(
    repo_full_name: str,
    *,
    amount: int = 1,
    new_status: Optional[str] = None,
    table_name: Optional[str] = None,
    **updates: Any,
) -> RepoState:
    """Atomically add ``amount`` to ``follow_up_count`` and refresh ``last_action_at``.

    REQ-6.4 requires ``follow_up_count`` to be incremented on every escalation.
    A read-modify-write would race a concurrent webhook event against a timer
    event (both can touch the same repo), so this uses a single DynamoDB
    ``ADD`` update expression for the counter, which is atomic on the server.
    The same call may also flip the status (e.g. ``dormant`` for the 14-day
    escalation, REQ-6.3) and set any other attribute, all in one write, and
    always stamps ``last_action_at`` (REQ-7.1).

    ``follow_up_count`` must not appear in ``updates`` — it is owned by the
    atomic ``ADD`` clause. ``new_status`` is validated against
    :data:`VALID_STATUSES`; unknown attribute names are rejected, mirroring
    :func:`transition`.

    Returns the post-update record (``ReturnValues="ALL_NEW"``). On a brand-new
    key DynamoDB treats ``ADD`` as starting from zero, so the first increment
    yields ``follow_up_count = amount``.
    """
    if new_status is not None and new_status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {new_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if "follow_up_count" in updates:
        raise ValueError(
            "follow_up_count is managed by the atomic ADD clause; do not pass it"
        )

    known = {f.name for f in fields(RepoState)}
    unknown = set(updates) - known
    if unknown:
        raise ValueError(f"unknown attribute(s): {sorted(unknown)}")

    now = _now_iso()
    set_attrs: dict[str, Any] = {"last_action_at": now, **updates}
    if new_status is not None:
        set_attrs["status"] = new_status
    set_attrs.pop("repo_full_name", None)

    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {}
    set_clauses: list[str] = []
    for i, (key, value) in enumerate(set_attrs.items()):
        name_ph = f"#s{i}"
        value_ph = f":s{i}"
        expr_names[name_ph] = key
        expr_values[value_ph] = value
        set_clauses.append(f"{name_ph} = {value_ph}")

    expr_names["#fc"] = "follow_up_count"
    expr_values[":inc"] = amount

    update_expression = (
        "SET " + ", ".join(set_clauses) + " ADD #fc :inc"
    )

    table = _get_table(table_name)
    response = table.update_item(
        Key={"repo_full_name": repo_full_name},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return RepoState.from_item(response["Attributes"])
