"""Unit tests for src.lambdas.scanner_lambda (Scanner: search -> dedup -> enqueue).

Everything runs offline: DynamoDB and SQS are mocked with moto, the GitHub
search is replaced by an injected fake, and Secrets Manager is never reached
because a ``GITHUB_TOKEN`` env var is set (or a stub client is injected).

Covers:
- the eligibility matrix (REQ-1.3, REQ-1.4) including the exact-30-day boundary
- MessageGroupId / deterministic MessageDeduplicationId (REQ-1.5)
- message body shape and JSON validity
- ``discovered`` state written after a successful enqueue (REQ-7.1)
- one failing candidate does not abort the run (design.md section 10)
- handler summary accuracy and the <=20-per-run cap (REQ-1.2)
- a clear configuration error when the queue URL is unset
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "TestResurrectorState"
QUEUE_NAME = "test-candidates.fifo"
REGION = "us-east-1"

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _candidate(module, name, *, stars=100, open_issues=25, branch="main"):
    """Build a RepoCandidate without touching PyGithub."""
    return module.RepoCandidate(
        repo_full_name=name,
        stars=stars,
        open_issues=open_issues,
        last_commit_at=NOW - timedelta(days=300),
        default_branch=branch,
        language="Python",
        html_url=f"https://github.com/{name}",
    )


class FakeSearch:
    """Stands in for ``github_search.search_candidates``; records its kwargs."""

    def __init__(self, candidates):
        self._candidates = candidates
        self.calls = []

    def __call__(self, *, client=None, token=None, max_results=None, now=None):
        self.calls.append(
            {"client": client, "token": token, "max_results": max_results, "now": now}
        )
        if max_results is not None:
            return list(self._candidates)[:max_results]
        return list(self._candidates)


class RecordingSqs:
    """Captures send_message kwargs instead of talking to SQS."""

    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    def send_message(self, **kwargs):
        if kwargs.get("MessageGroupId") in self.fail_on:
            raise RuntimeError("simulated SQS failure")
        self.sent.append(kwargs)
        return {"MessageId": f"msg-{len(self.sent)}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner(monkeypatch):
    """Reload the module with test configuration in place."""
    monkeypatch.setenv("RESURRECTOR_STATE_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("RESURRECTOR_CANDIDATE_QUEUE_URL", "https://sqs.test/queue.fifo")
    monkeypatch.delenv("RESURRECTOR_REEVALUATION_DAYS", raising=False)
    monkeypatch.delenv("RESURRECTOR_DEDUP_WINDOW_SECONDS", raising=False)

    import src.tools.dynamo_tools as dt
    import src.lambdas.scanner_lambda as sl

    importlib.reload(dt)
    importlib.reload(sl)
    sl.reset_clients()
    yield sl
    sl.reset_clients()


@pytest.fixture
def dynamo(scanner):
    """Create the mocked state table for the duration of a test."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "repo_full_name", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "repo_full_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# Eligibility matrix (REQ-1.3, REQ-1.4)
# ---------------------------------------------------------------------------


def test_missing_record_is_eligible(scanner):
    assert scanner.is_eligible(None, now=NOW) is True


@pytest.mark.parametrize("status", ["in_progress", "pr_opened"])
def test_active_statuses_are_never_eligible(scanner, status):
    """REQ-1.3: never re-enqueue in_progress / pr_opened repos."""
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status=status,
        last_action_at=(NOW - timedelta(days=365)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is False


def test_active_followup_is_not_eligible(scanner):
    """A pr_opened repo mid-escalation stays blocked regardless of age."""
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status="pr_opened",
        follow_up_count=1,
        maintainer_responded=False,
        last_action_at=(NOW - timedelta(days=400)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is False


@pytest.mark.parametrize(
    "status",
    ["ignored", "rejected", "success", "dormant", "skipped_complex", "fix_failed"],
)
def test_closed_statuses_eligible_after_30_days(scanner, status):
    """REQ-1.4: closed (-> rejected) or ignored older than 30 days is re-eligible."""
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status=status,
        last_action_at=(NOW - timedelta(days=31)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is True


@pytest.mark.parametrize("status", ["ignored", "rejected", "dormant", "discovered"])
def test_closed_statuses_not_eligible_within_30_days(scanner, status):
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status=status,
        last_action_at=(NOW - timedelta(days=29)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is False


def test_exactly_thirty_days_is_not_yet_eligible(scanner):
    """"More than 30 days" is strict: the boundary itself stays blocked."""
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status="ignored",
        last_action_at=(NOW - timedelta(days=30)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is False
    # One second past the boundary flips it.
    state.last_action_at = (NOW - timedelta(days=30, seconds=1)).isoformat()
    assert scanner.is_eligible(state, now=NOW) is True


def test_reevaluation_window_is_configurable(scanner, monkeypatch):
    state = scanner.RepoState(
        repo_full_name="owner/repo",
        status="rejected",
        last_action_at=(NOW - timedelta(days=10)).isoformat(),
    )
    assert scanner.is_eligible(state, now=NOW) is False
    assert scanner.is_eligible(state, now=NOW, reevaluation_days=7) is True

    monkeypatch.setenv(scanner.REEVALUATION_DAYS_ENV_VAR, "7")
    assert scanner.is_eligible(state, now=NOW) is True


def test_missing_last_action_at_is_not_eligible(scanner):
    state = scanner.RepoState(repo_full_name="owner/repo", status="ignored")
    assert scanner.is_eligible(state, now=NOW) is False


# ---------------------------------------------------------------------------
# Message body and dedup id (REQ-1.5)
# ---------------------------------------------------------------------------


def test_message_body_is_valid_json_with_expected_fields(scanner):
    body = scanner.build_message_body(
        _candidate(scanner, "owner/repo", stars=42, open_issues=33, branch="trunk"),
        discovered_at=NOW,
    )
    payload = json.loads(body)
    assert payload["repo_full_name"] == "owner/repo"
    assert payload["default_branch"] == "trunk"
    assert payload["open_issues"] == 33
    assert payload["stars"] == 42
    assert payload["html_url"] == "https://github.com/owner/repo"
    assert payload["language"] == "Python"
    assert payload["discovered_at"] == NOW.isoformat()


def test_dedup_id_is_deterministic_within_a_window(scanner):
    first = scanner.build_deduplication_id("owner/repo", now=NOW, window_seconds=21600)
    second = scanner.build_deduplication_id(
        "owner/repo", now=NOW + timedelta(minutes=5), window_seconds=21600
    )
    assert first == second
    assert len(first) == 64  # sha256 hex, inside the SQS 128-char limit


def test_dedup_id_changes_in_a_later_window_and_per_repo(scanner):
    base = scanner.build_deduplication_id("owner/repo", now=NOW, window_seconds=21600)
    later = scanner.build_deduplication_id(
        "owner/repo", now=NOW + timedelta(hours=7), window_seconds=21600
    )
    other_repo = scanner.build_deduplication_id(
        "owner/other", now=NOW, window_seconds=21600
    )
    assert base != later
    assert base != other_repo


def test_enqueue_uses_repo_full_name_as_message_group_id(scanner):
    sqs = RecordingSqs()
    scanner.enqueue_candidate(
        _candidate(scanner, "owner/repo"),
        queue_url="https://sqs.test/queue.fifo",
        sqs_client=sqs,
        now=NOW,
    )
    assert len(sqs.sent) == 1
    sent = sqs.sent[0]
    assert sent["QueueUrl"] == "https://sqs.test/queue.fifo"
    assert sent["MessageGroupId"] == "owner/repo"
    assert sent["MessageDeduplicationId"] == scanner.build_deduplication_id(
        "owner/repo", now=NOW
    )
    assert json.loads(sent["MessageBody"])["repo_full_name"] == "owner/repo"


def test_missing_queue_url_raises_configuration_error(scanner, monkeypatch):
    monkeypatch.delenv(scanner.QUEUE_URL_ENV_VAR, raising=False)
    with pytest.raises(scanner.ConfigurationError):
        scanner.enqueue_candidate(
            _candidate(scanner, "owner/repo"), sqs_client=RecordingSqs(), now=NOW
        )
    with pytest.raises(scanner.ConfigurationError):
        scanner.scan(github_client=object(), sqs_client=RecordingSqs(), now=NOW)


# ---------------------------------------------------------------------------
# Real SQS FIFO round-trip via moto
# ---------------------------------------------------------------------------


def test_enqueue_against_moto_fifo_queue(scanner):
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(
            QueueName=QUEUE_NAME,
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )["QueueUrl"]

        scanner.enqueue_candidate(
            _candidate(scanner, "owner/repo"),
            queue_url=queue_url,
            sqs_client=sqs,
            now=NOW,
        )

        received = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            AttributeNames=["MessageGroupId"],
        )
        messages = received.get("Messages", [])
        assert len(messages) == 1
        assert json.loads(messages[0]["Body"])["repo_full_name"] == "owner/repo"


# ---------------------------------------------------------------------------
# scan() end-to-end (offline)
# ---------------------------------------------------------------------------


def test_scan_enqueues_new_candidate_and_writes_discovered_state(scanner, dynamo):
    sqs = RecordingSqs()
    search = FakeSearch([_candidate(scanner, "owner/new")])

    summary = scanner.scan(
        github_client=object(),
        sqs_client=sqs,
        now=NOW,
        search=search,
    )

    assert summary["scanned"] == 1
    assert summary["eligible"] == 1
    assert summary["enqueued"] == 1
    assert summary["skipped"] == 0
    assert summary["errors"] == 0
    assert summary["enqueued_repos"] == ["owner/new"]
    assert [s["MessageGroupId"] for s in sqs.sent] == ["owner/new"]

    state = scanner.dynamo_tools.read_state("owner/new")
    assert state is not None
    assert state.status == "discovered"
    assert state.last_action_at is not None


def test_scan_skips_in_progress_repo(scanner, dynamo):
    scanner.dynamo_tools.write_state(
        scanner.RepoState(
            repo_full_name="owner/busy",
            status="in_progress",
            last_action_at=(NOW - timedelta(days=1)).isoformat(),
        )
    )
    sqs = RecordingSqs()
    summary = scanner.scan(
        github_client=object(),
        sqs_client=sqs,
        now=NOW,
        search=FakeSearch([_candidate(scanner, "owner/busy")]),
    )

    assert summary["enqueued"] == 0
    assert summary["skipped"] == 1
    assert summary["skipped_repos"] == ["owner/busy"]
    assert sqs.sent == []


def test_scan_reenqueues_repo_ignored_long_ago(scanner, dynamo):
    """REQ-1.4 through the full flow."""
    scanner.dynamo_tools.write_state(
        scanner.RepoState(
            repo_full_name="owner/stale",
            status="ignored",
            last_action_at=(NOW - timedelta(days=45)).isoformat(),
        )
    )
    sqs = RecordingSqs()
    summary = scanner.scan(
        github_client=object(),
        sqs_client=sqs,
        now=NOW,
        search=FakeSearch([_candidate(scanner, "owner/stale")]),
    )

    assert summary["enqueued"] == 1
    assert [s["MessageGroupId"] for s in sqs.sent] == ["owner/stale"]
    assert scanner.dynamo_tools.read_state("owner/stale").status == "discovered"


def test_scan_continues_after_one_candidate_fails(scanner, dynamo):
    """design.md section 10: a per-candidate failure must not abort the run."""
    sqs = RecordingSqs(fail_on={"owner/boom"})
    search = FakeSearch(
        [
            _candidate(scanner, "owner/a"),
            _candidate(scanner, "owner/boom"),
            _candidate(scanner, "owner/b"),
        ]
    )

    summary = scanner.scan(
        github_client=object(), sqs_client=sqs, now=NOW, search=search
    )

    assert summary["scanned"] == 3
    assert summary["enqueued"] == 2
    assert summary["errors"] == 1
    assert summary["error_repos"] == ["owner/boom"]
    assert [s["MessageGroupId"] for s in sqs.sent] == ["owner/a", "owner/b"]
    # The failed candidate has no state record, so a later scan retries it.
    assert scanner.dynamo_tools.read_state("owner/boom") is None


def test_scan_summary_counts_mixed_outcomes(scanner, dynamo):
    scanner.dynamo_tools.write_state(
        scanner.RepoState(
            repo_full_name="owner/skip",
            status="pr_opened",
            last_action_at=(NOW - timedelta(days=2)).isoformat(),
        )
    )
    sqs = RecordingSqs(fail_on={"owner/boom"})
    search = FakeSearch(
        [
            _candidate(scanner, "owner/new"),
            _candidate(scanner, "owner/skip"),
            _candidate(scanner, "owner/boom"),
        ]
    )

    summary = scanner.scan(
        github_client=object(), sqs_client=sqs, now=NOW, search=search
    )

    assert summary == {
        "scanned": 3,
        "eligible": 2,
        "enqueued": 1,
        "skipped": 1,
        "errors": 1,
        "enqueued_repos": ["owner/new"],
        "skipped_repos": ["owner/skip"],
        "error_repos": ["owner/boom"],
    }
    # Summary must be JSON-serializable for the CloudWatch log.
    assert json.loads(json.dumps(summary)) == summary


def test_scan_passes_max_results_cap_to_search(scanner, dynamo):
    """REQ-1.2: at most 20 per run."""
    search = FakeSearch([_candidate(scanner, f"owner/r{i}") for i in range(50)])
    summary = scanner.scan(
        github_client=object(),
        sqs_client=RecordingSqs(),
        now=NOW,
        max_results=20,
        search=search,
    )
    assert search.calls[0]["max_results"] == 20
    assert summary["scanned"] == 20
    assert summary["enqueued"] == 20


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_lambda_handler_returns_summary(scanner, dynamo, monkeypatch):
    sqs = RecordingSqs()
    search = FakeSearch([_candidate(scanner, "owner/new")])
    real_scan = scanner.scan
    captured = {}

    def fake_scan(**kwargs):
        captured.update(kwargs)
        return real_scan(
            github_client=object(), sqs_client=sqs, now=NOW, search=search, **kwargs
        )

    monkeypatch.setattr(scanner, "scan", fake_scan)

    result = scanner.lambda_handler({"max_results": 5}, None)
    assert captured == {"max_results": 5}
    assert result["enqueued"] == 1
    assert result["scanned"] == 1


def test_lambda_handler_ignores_unknown_event_keys(scanner, monkeypatch):
    """A plain CloudWatch event carries no overrides."""
    captured = {}

    def fake_scan(**kwargs):
        captured.update(kwargs)
        return {"scanned": 0}

    monkeypatch.setattr(scanner, "scan", fake_scan)
    result = scanner.lambda_handler({"source": "aws.events", "detail": {}}, None)
    assert result == {"scanned": 0}
    assert captured == {}


# ---------------------------------------------------------------------------
# GitHub token loading (design.md sections 6 and 11)
# ---------------------------------------------------------------------------


def test_load_github_token_prefers_env_var(scanner):
    assert scanner.load_github_token(use_cache=False) == "test-token"


def test_load_github_token_reads_plain_secret(scanner, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class Secrets:
        def __init__(self):
            self.calls = []

        def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg name
            self.calls.append(SecretId)
            return {"SecretString": "ghp_plain"}

    secrets = Secrets()
    token = scanner.load_github_token(secrets_client=secrets, use_cache=False)
    assert token == "ghp_plain"
    assert secrets.calls == [scanner.DEFAULT_GITHUB_TOKEN_SECRET_ID]


def test_load_github_token_reads_json_secret_and_caches(scanner, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class Secrets:
        def __init__(self):
            self.call_count = 0

        def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg name
            self.call_count += 1
            return {"SecretString": json.dumps({"token": "ghp_json"})}

    secrets = Secrets()
    assert scanner.load_github_token(secrets_client=secrets) == "ghp_json"
    # Second call is served from the per-container cache.
    assert scanner.load_github_token(secrets_client=secrets) == "ghp_json"
    assert secrets.call_count == 1


def test_load_github_token_rejects_empty_secret(scanner, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class Secrets:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg name
            return {"SecretString": ""}

    with pytest.raises(scanner.ConfigurationError):
        scanner.load_github_token(secrets_client=Secrets(), use_cache=False)
