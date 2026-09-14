"""Unit tests for src.lambdas.dashboard_lambda (GET /state -> JSON).

Everything runs fully offline: DynamoDB is mocked with moto, and the paginated
/failure scans use small deterministic fake resources. No network, no strands,
no Bedrock.

Covers:
- get_dashboard_data: scan returns all rows as JSON-safe dicts (no Decimal
  leaks; json.dumps succeeds); each row has the REQ-7.3 fields.
- pagination: a fake resource returning two pages then none -> all items, no
  drops/dupes, and ExclusiveStartKey is threaded through.
- Decimal coercion: numeric attrs -> ints; json.dumps does not raise.
- empty table -> repos: [], count 0, still 200.
- lambda_handler: 200, Content-Type application/json, CORS header, expected
  shape, newest-last_action_at-first sort.
- scan failure (injected client whose scan raises) -> 500 JSON error, no crash.
- boundary (AST): the module never calls put_item/update_item/transition/
  write_state and does not import strands.
- index.html: 30s interval, REQ-7.3 field references, textContent-based
  rendering (no innerHTML= of scanned data), configurable API URL.
"""

from __future__ import annotations

import ast
import importlib
import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "TestResurrectorState"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard(monkeypatch):
    """Provide the dashboard module configured for the mocked test table."""
    monkeypatch.setenv("RESURRECTOR_STATE_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import src.tools.dynamo_tools as dt
    import src.lambdas.dashboard_lambda as mod

    importlib.reload(dt)
    importlib.reload(mod)
    mod.reset_clients()
    return mod


@pytest.fixture
def table():
    """Create the mocked DynamoDB table for the duration of a test."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "repo_full_name", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "repo_full_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table(TABLE_NAME)


def _put(table, **attrs):
    table.put_item(Item=attrs)


# ---------------------------------------------------------------------------
# Fake resources for pagination / failure (deterministic, no moto quirks)
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, pages):
        # pages: list of dicts already shaped like a scan() response
        self._pages = pages
        self.scan_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        # Return the page whose ExclusiveStartKey matches the call order.
        idx = len(self.scan_calls) - 1
        return self._pages[idx]


class _FakeResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):  # noqa: N802 - mirror boto3 resource API
        return self._table


class _RaisingTable:
    def scan(self, **kwargs):
        raise RuntimeError("simulated scan failure")


class _RaisingResource:
    def Table(self, name):  # noqa: N802
        return _RaisingTable()


# ---------------------------------------------------------------------------
# get_dashboard_data — happy path
# ---------------------------------------------------------------------------


def test_scan_returns_json_safe_rows_with_required_fields(dashboard, table):
    _put(
        table,
        repo_full_name="owner/alpha",
        status="pr_opened",
        pr_url="https://github.com/owner/alpha/pull/7",
        pr_number=Decimal("7"),
        issue_number=Decimal("3"),
        follow_up_count=Decimal("1"),
        maintainer_responded=True,
        last_action_at="2024-01-02T00:00:00+00:00",
    )
    _put(
        table,
        repo_full_name="owner/beta",
        status="discovered",
        maintainer_responded=False,
        last_action_at="2024-01-01T00:00:00+00:00",
    )

    rows = dashboard.get_dashboard_data()

    # json.dumps must not raise -> proves no Decimal leaked through.
    json.dumps(rows)

    assert len(rows) == 2
    for row in rows:
        for field in (
            "repo_full_name",
            "status",
            "last_action_at",
            "maintainer_responded",
        ):
            assert field in row
    alpha = next(r for r in rows if r["repo_full_name"] == "owner/alpha")
    assert alpha["pr_url"] == "https://github.com/owner/alpha/pull/7"
    assert isinstance(alpha["pr_number"], int) and alpha["pr_number"] == 7
    assert isinstance(alpha["issue_number"], int)
    assert isinstance(alpha["follow_up_count"], int)
    assert alpha["maintainer_responded"] is True


def test_scan_sorts_newest_last_action_first(dashboard, table):
    _put(table, repo_full_name="owner/old", status="dormant",
         last_action_at="2024-01-01T00:00:00+00:00")
    _put(table, repo_full_name="owner/new", status="success",
         last_action_at="2024-06-01T00:00:00+00:00")
    _put(table, repo_full_name="owner/mid", status="rejected",
         last_action_at="2024-03-01T00:00:00+00:00")

    rows = dashboard.get_dashboard_data()
    order = [r["repo_full_name"] for r in rows]
    assert order == ["owner/new", "owner/mid", "owner/old"]


def test_scan_rows_missing_timestamp_sort_last(dashboard, table):
    _put(table, repo_full_name="owner/has-ts", status="success",
         last_action_at="2024-01-01T00:00:00+00:00")
    _put(table, repo_full_name="owner/no-ts", status="discovered")

    rows = dashboard.get_dashboard_data()
    assert rows[0]["repo_full_name"] == "owner/has-ts"
    assert rows[-1]["repo_full_name"] == "owner/no-ts"


def test_empty_table_returns_empty_list(dashboard, table):
    assert dashboard.get_dashboard_data() == []


# ---------------------------------------------------------------------------
# Pagination — deterministic two-page fake
# ---------------------------------------------------------------------------


def test_pagination_follows_last_evaluated_key(dashboard):
    page1 = {
        "Items": [
            {"repo_full_name": "owner/a", "status": "discovered"},
            {"repo_full_name": "owner/b", "status": "pr_opened"},
        ],
        "LastEvaluatedKey": {"repo_full_name": "owner/b"},
    }
    page2 = {
        "Items": [
            {"repo_full_name": "owner/c", "status": "success"},
        ],
        # No LastEvaluatedKey -> loop terminates.
    }
    fake_table = _FakeTable([page1, page2])
    resource = _FakeResource(fake_table)

    rows = dashboard.get_dashboard_data(ddb_resource=resource)
    names = sorted(r["repo_full_name"] for r in rows)

    assert names == ["owner/a", "owner/b", "owner/c"]  # all pages, no dupes/drops
    assert len(fake_table.scan_calls) == 2
    # Second scan must carry the ExclusiveStartKey from page 1.
    assert fake_table.scan_calls[0] == {}
    assert fake_table.scan_calls[1]["ExclusiveStartKey"] == {"repo_full_name": "owner/b"}


def test_decimal_attrs_coerced_to_int(dashboard):
    page = {
        "Items": [
            {
                "repo_full_name": "owner/nums",
                "status": "pr_opened",
                "issue_number": Decimal("12"),
                "pr_number": Decimal("34"),
                "follow_up_count": Decimal("2"),
            }
        ]
    }
    resource = _FakeResource(_FakeTable([page]))
    rows = dashboard.get_dashboard_data(ddb_resource=resource)

    json.dumps(rows)  # must not raise
    row = rows[0]
    assert row["issue_number"] == 12 and isinstance(row["issue_number"], int)
    assert row["pr_number"] == 34 and isinstance(row["pr_number"], int)
    assert row["follow_up_count"] == 2 and isinstance(row["follow_up_count"], int)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_lambda_handler_shape_and_headers(dashboard, table):
    _put(table, repo_full_name="owner/new", status="success",
         pr_url="https://github.com/owner/new/pull/1", pr_number=Decimal("1"),
         maintainer_responded=True, last_action_at="2024-06-01T00:00:00+00:00")
    _put(table, repo_full_name="owner/old", status="dormant",
         maintainer_responded=False, last_action_at="2024-01-01T00:00:00+00:00")

    resp = dashboard.lambda_handler({}, None)

    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    body = json.loads(resp["body"])
    assert body["count"] == 2
    assert "generated_at" in body
    assert [r["repo_full_name"] for r in body["repos"]] == ["owner/new", "owner/old"]


def test_lambda_handler_empty_table(dashboard, table):
    resp = dashboard.lambda_handler({}, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 0
    assert body["repos"] == []


def test_lambda_handler_scan_failure_returns_500(dashboard):
    resp = dashboard.lambda_handler({}, None, ddb_resource=_RaisingResource())
    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert "error" in body
    assert resp["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# Boundary — read-only (AST)
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = Path(__file__).resolve().parents[1] / "src" / "lambdas" / "dashboard_lambda.py"
    return path.read_text(encoding="utf-8")


def test_dashboard_never_calls_write_operations():
    tree = ast.parse(_module_source())
    forbidden = {"put_item", "update_item", "delete_item", "transition", "write_state",
                 "increment_follow_up"}
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            called.add(node.attr)
        if isinstance(node, ast.Name):
            called.add(node.id)
    leaked = forbidden & called
    assert not leaked, f"dashboard must be read-only; found write ops: {leaked}"


def test_dashboard_does_not_import_strands():
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("strands")
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("strands")


def test_dashboard_only_scan_boto_action():
    # The only DynamoDB verb used is scan (read-only).
    source = _module_source()
    assert ".scan(" in source
    for write_verb in (".put_item(", ".update_item(", ".delete_item("):
        assert write_verb not in source


# ---------------------------------------------------------------------------
# index.html — pragmatic structural checks
# ---------------------------------------------------------------------------


def _index_html() -> str:
    path = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "index.html"
    return path.read_text(encoding="utf-8")


def test_index_html_exists_and_auto_refreshes():
    html = _index_html()
    assert "setInterval" in html
    assert "30000" in html  # 30-second refresh (REQ-7.3)


def test_index_html_references_required_fields():
    html = _index_html()
    for field in ("status", "pr_url", "last_action_at", "maintainer_responded"):
        assert field in html


def test_index_html_uses_safe_rendering():
    html = _index_html()
    # textContent-based rendering present.
    assert "textContent" in html
    # No innerHTML assignment of scanned data.
    assert "innerHTML =" not in html
    assert "innerHTML=" not in html


def test_index_html_api_url_is_configurable():
    html = _index_html()
    # At least one of the documented injection mechanisms is present.
    assert "DASHBOARD_API_URL" in html or "dashboard-api" in html or "?api=" in html
    assert "/state" in html


def test_index_html_safe_wording():
    html = _index_html().lower()
    assert "reduced maintenance activity" in html
    assert "abandoned" not in html
    assert "dead repo" not in html.replace("dead repo resurrector", "")
