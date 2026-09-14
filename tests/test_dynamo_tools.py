"""Unit tests for src.tools.dynamo_tools (DynamoDB helpers).

Uses moto to mock DynamoDB so tests exercise real serialization/deserialization
against an in-memory table without touching AWS.
"""

from __future__ import annotations

import importlib

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "TestResurrectorState"


@pytest.fixture
def dynamo_tools(monkeypatch):
    """Provide the module configured to use the mocked test table."""
    monkeypatch.setenv("RESURRECTOR_STATE_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import src.tools.dynamo_tools as dt

    importlib.reload(dt)
    return dt


@pytest.fixture
def table(dynamo_tools):
    """Create the mocked DynamoDB table for the duration of a test."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "repo_full_name", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "repo_full_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client.Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# RepoState dataclass
# ---------------------------------------------------------------------------


def test_repostate_defaults(dynamo_tools):
    state = dynamo_tools.RepoState(repo_full_name="owner/repo")
    assert state.status == "discovered"
    assert state.maintainer_responded is False
    assert state.follow_up_count == 0
    assert state.issue_number is None


def test_to_item_drops_none(dynamo_tools):
    state = dynamo_tools.RepoState(repo_full_name="owner/repo")
    item = state.to_item()
    assert "repo_full_name" in item
    assert "status" in item
    # Optional None-valued attributes are omitted.
    assert "issue_number" not in item
    assert "pr_url" not in item


def test_from_item_coerces_decimals(dynamo_tools):
    from decimal import Decimal

    item = {
        "repo_full_name": "owner/repo",
        "status": "pr_opened",
        "issue_number": Decimal("42"),
        "pr_number": Decimal("7"),
        "follow_up_count": Decimal("2"),
        "unknown_attr": "ignored",
    }
    state = dynamo_tools.RepoState.from_item(item)
    assert state.issue_number == 42
    assert isinstance(state.issue_number, int)
    assert state.pr_number == 7
    assert state.follow_up_count == 2


# ---------------------------------------------------------------------------
# read_state / write_state
# ---------------------------------------------------------------------------


def test_read_state_missing_returns_none(dynamo_tools, table):
    assert dynamo_tools.read_state("owner/absent") is None


def test_write_then_read_roundtrip(dynamo_tools, table):
    state = dynamo_tools.RepoState(
        repo_full_name="owner/repo",
        status="pr_opened",
        issue_number=42,
        pr_number=7,
        pr_url="https://github.com/owner/repo/pull/7",
        opened_at="2024-01-01T00:00:00+00:00",
    )
    dynamo_tools.write_state(state)

    loaded = dynamo_tools.read_state("owner/repo")
    assert loaded is not None
    assert loaded.status == "pr_opened"
    assert loaded.issue_number == 42
    assert loaded.pr_number == 7
    assert loaded.pr_url == "https://github.com/owner/repo/pull/7"


def test_write_state_stamps_last_action_at(dynamo_tools, table):
    state = dynamo_tools.RepoState(repo_full_name="owner/repo")
    assert state.last_action_at is None
    dynamo_tools.write_state(state)
    assert state.last_action_at is not None
    loaded = dynamo_tools.read_state("owner/repo")
    assert loaded.last_action_at is not None


# ---------------------------------------------------------------------------
# transition (REQ-4.4, REQ-7.1)
# ---------------------------------------------------------------------------


def test_transition_updates_status_and_last_action_at(dynamo_tools, table):
    dynamo_tools.write_state(
        dynamo_tools.RepoState(
            repo_full_name="owner/repo",
            status="in_progress",
            last_action_at="2020-01-01T00:00:00+00:00",
        )
    )
    result = dynamo_tools.transition("owner/repo", "pr_opened")
    assert result.status == "pr_opened"
    # last_action_at must be refreshed (REQ-7.1).
    assert result.last_action_at != "2020-01-01T00:00:00+00:00"


def test_transition_applies_pr_opened_attributes(dynamo_tools, table):
    """REQ-4.4: pr_opened records pr_number, pr_url, issue_number, opened_at."""
    dynamo_tools.write_state(
        dynamo_tools.RepoState(repo_full_name="owner/repo", status="in_progress")
    )
    result = dynamo_tools.transition(
        "owner/repo",
        "pr_opened",
        pr_number=7,
        pr_url="https://github.com/owner/repo/pull/7",
        issue_number=42,
        opened_at="2024-02-02T00:00:00+00:00",
    )
    assert result.status == "pr_opened"
    assert result.pr_number == 7
    assert result.pr_url == "https://github.com/owner/repo/pull/7"
    assert result.issue_number == 42
    assert result.opened_at == "2024-02-02T00:00:00+00:00"

    loaded = dynamo_tools.read_state("owner/repo")
    assert loaded.pr_number == 7
    assert loaded.issue_number == 42


def test_transition_creates_record_when_absent(dynamo_tools, table):
    result = dynamo_tools.transition("owner/new", "discovered")
    assert result.status == "discovered"
    assert result.last_action_at is not None


def test_transition_rejects_invalid_status(dynamo_tools, table):
    with pytest.raises(ValueError):
        dynamo_tools.transition("owner/repo", "bogus_status")


def test_transition_rejects_unknown_attribute(dynamo_tools, table):
    with pytest.raises(ValueError):
        dynamo_tools.transition("owner/repo", "pr_opened", not_a_field=1)


# ---------------------------------------------------------------------------
# increment_follow_up (REQ-6.4 atomic counter)
# ---------------------------------------------------------------------------


def test_increment_follow_up_starts_from_zero(dynamo_tools, table):
    result = dynamo_tools.increment_follow_up("owner/repo")
    assert result.follow_up_count == 1
    assert result.last_action_at is not None


def test_increment_follow_up_is_additive(dynamo_tools, table):
    """Two increments yield +2 without a read-modify-write race."""
    dynamo_tools.increment_follow_up("owner/repo")
    result = dynamo_tools.increment_follow_up("owner/repo")
    assert result.follow_up_count == 2

    loaded = dynamo_tools.read_state("owner/repo")
    assert loaded.follow_up_count == 2


def test_increment_follow_up_can_set_status_and_attributes(dynamo_tools, table):
    dynamo_tools.write_state(
        dynamo_tools.RepoState(repo_full_name="owner/repo", status="pr_opened")
    )
    result = dynamo_tools.increment_follow_up(
        "owner/repo", new_status="dormant", notes="no response"
    )
    assert result.status == "dormant"
    assert result.follow_up_count == 1
    assert result.notes == "no response"


def test_increment_follow_up_rejects_bad_status(dynamo_tools, table):
    with pytest.raises(ValueError):
        dynamo_tools.increment_follow_up("owner/repo", new_status="bogus")


def test_increment_follow_up_rejects_counter_in_updates(dynamo_tools, table):
    with pytest.raises(ValueError):
        dynamo_tools.increment_follow_up("owner/repo", follow_up_count=5)


def test_increment_follow_up_rejects_unknown_attribute(dynamo_tools, table):
    with pytest.raises(ValueError):
        dynamo_tools.increment_follow_up("owner/repo", not_a_field=1)
