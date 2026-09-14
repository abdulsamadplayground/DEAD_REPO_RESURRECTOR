"""Unit tests for src.lambdas.processor_lambda (SQS -> Orchestrator + side effects).

Everything runs fully offline: the Orchestrator ``run`` / ``handle_reply`` calls
are replaced by injected fakes, SQS/SNS are either injected fakes or moto, and
the diagnostic note writer is injected. No network, no Bedrock, no real AWS.

Covers:
- routing: candidate body -> run; message_type="follow_up" body -> handle_reply
- REQ-6.1: a 7-day follow-up intent is enqueued (group id, dedup id, stage, delay)
- REQ-6.2: a fired 7-day timer -> 14-day enqueue + SNS publish
- REQ-6.4: an SNS intent publishes; no intent -> no publish
- the SQS DelaySeconds > 900 caveat is handled honestly (not silently sent)
- partial batch failure reporting (only the bad record is redriven)
- error handling: run failure -> batch failure + best-effort note that cannot mask
- no-op results enqueue/publish nothing and are not failures
- malformed JSON bodies are reported as failures, not crashes
- boundary: the Processor never calls dynamo_tools.transition / write_state
"""

from __future__ import annotations

import ast
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from src.agents.orchestrator import OrchestrationResult

REGION = "us-east-1"
QUEUE_NAME = "test-candidates.fifo"
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingSqs:
    """Captures send_message kwargs instead of talking to SQS."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("simulated SQS failure")
        self.sent.append(kwargs)
        return {"MessageId": f"msg-{len(self.sent)}"}


class RecordingSns:
    """Captures publish kwargs instead of talking to SNS."""

    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": f"sns-{len(self.published)}"}


class FakeScheduler:
    """Records schedule() calls; returns a serializable stub result."""

    def __init__(self):
        self.calls = []

    def schedule(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"scheduled": False, **kwargs})


class FakeSnsPublisher:
    """Records publish() intents."""

    def __init__(self):
        self.intents = []

    def publish(self, intent):
        self.intents.append(intent)
        return {"MessageId": "sns-1"}


def _record(body_obj, message_id="m1"):
    """Build an SQS record with a JSON body."""
    return {"messageId": message_id, "body": json.dumps(body_obj)}


def _candidate_body(repo="owner/repo"):
    return {"repo_full_name": repo, "default_branch": "main", "open_issues": 25}


def _follow_up_body(repo="owner/repo", stage=7, pr=42, issue=7):
    return {
        "message_type": "follow_up",
        "stage": stage,
        "repo_full_name": repo,
        "pr_number": pr,
        "issue_number": issue,
    }


def _seven_day_intent():
    return {"delay_seconds": 604800, "message_type": "follow_up", "stage": 7}


def _fourteen_day_intent():
    return {"delay_seconds": 1209600, "message_type": "follow_up", "stage": 14}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def processor(monkeypatch):
    """Reload the module with test configuration in place."""
    monkeypatch.setenv("RESURRECTOR_CANDIDATE_QUEUE_URL", "https://sqs.test/queue.fifo")
    monkeypatch.setenv("RESURRECTOR_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("RESURRECTOR_FOLLOWUP_DEDUP_WINDOW_SECONDS", raising=False)

    import src.lambdas.processor_lambda as pl

    importlib.reload(pl)
    pl.reset_clients()
    yield pl
    pl.reset_clients()


# ---------------------------------------------------------------------------
# Routing (design.md section 5)
# ---------------------------------------------------------------------------


def test_candidate_body_routes_to_run(processor):
    run_calls, reply_calls = [], []

    def fake_run(msg):
        run_calls.append(msg)
        return OrchestrationResult(repo_full_name=msg["repo_full_name"], status="pr_opened", stage_reached="done")

    def fake_reply(evt):
        reply_calls.append(evt)
        return OrchestrationResult(repo_full_name=evt["repo_full_name"], status="pr_opened", stage_reached="reply")

    result = processor.lambda_handler(
        {"Records": [_record(_candidate_body("owner/a"))]},
        None,
        run_fn=fake_run,
        handle_reply_fn=fake_reply,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
    )
    assert run_calls == [_candidate_body("owner/a")]
    assert reply_calls == []
    assert result == {"batchItemFailures": []}


def test_follow_up_body_routes_to_handle_reply(processor):
    run_calls, reply_calls = [], []

    def fake_run(msg):
        run_calls.append(msg)
        return OrchestrationResult(repo_full_name=msg["repo_full_name"], status="pr_opened", stage_reached="done")

    def fake_reply(evt):
        reply_calls.append(evt)
        return OrchestrationResult(repo_full_name=evt["repo_full_name"], status="pr_opened", stage_reached="follow_up")

    processor.lambda_handler(
        {"Records": [_record(_follow_up_body("owner/b", stage=7))]},
        None,
        run_fn=fake_run,
        handle_reply_fn=fake_reply,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
    )
    assert run_calls == []
    assert reply_calls == [_follow_up_body("owner/b", stage=7)]


# ---------------------------------------------------------------------------
# REQ-6.1 — 7-day follow-up enqueue
# ---------------------------------------------------------------------------


def test_run_result_enqueues_seven_day_follow_up(processor):
    scheduler = FakeScheduler()
    sns = FakeSnsPublisher()

    def fake_run(msg):
        return OrchestrationResult(
            repo_full_name="owner/repo",
            status="pr_opened",
            stage_reached="done",
            issue_number=7,
            pr_number=42,
            follow_up=_seven_day_intent(),
        )

    processor.lambda_handler(
        {"Records": [_record(_candidate_body("owner/repo"))]},
        None,
        run_fn=fake_run,
        handle_reply_fn=lambda e: None,
        scheduler=scheduler,
        sns_publisher=sns,
    )
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["repo_full_name"] == "owner/repo"
    assert call["delay_seconds"] == 604800
    assert call["stage"] == 7
    assert call["pr_number"] == 42
    assert call["issue_number"] == 7
    # A 7-day intent carries no SNS on the run() path, so nothing is published.
    assert sns.intents == []


# ---------------------------------------------------------------------------
# REQ-6.2 — fired 7-day timer -> 14-day enqueue + SNS
# ---------------------------------------------------------------------------


def test_seven_day_timer_enqueues_fourteen_day_and_publishes_sns(processor):
    scheduler = FakeScheduler()
    sns = FakeSnsPublisher()

    def fake_reply(evt):
        assert evt["message_type"] == "follow_up"
        return OrchestrationResult(
            repo_full_name="owner/repo",
            status="pr_opened",
            stage_reached="follow_up",
            issue_number=7,
            pr_number=42,
            follow_up_count=1,
            follow_up=_fourteen_day_intent(),
            sns={"subject": "[Resurrector] 7-day follow-up on owner/repo", "message": "posted"},
        )

    processor.lambda_handler(
        {"Records": [_record(_follow_up_body("owner/repo", stage=7))]},
        None,
        run_fn=lambda m: None,
        handle_reply_fn=fake_reply,
        scheduler=scheduler,
        sns_publisher=sns,
    )
    assert len(scheduler.calls) == 1
    assert scheduler.calls[0]["stage"] == 14
    assert scheduler.calls[0]["delay_seconds"] == 1209600
    assert len(sns.intents) == 1
    assert sns.intents[0]["subject"].startswith("[Resurrector]")


def test_fourteen_day_timer_publishes_sns_and_enqueues_nothing(processor):
    scheduler = FakeScheduler()
    sns = FakeSnsPublisher()

    def fake_reply(evt):
        return OrchestrationResult(
            repo_full_name="owner/repo",
            status="dormant",
            stage_reached="follow_up",
            pr_number=42,
            follow_up_count=2,
            sns={"subject": "[Resurrector] owner/repo marked dormant", "message": "dormant"},
        )

    processor.lambda_handler(
        {"Records": [_record(_follow_up_body("owner/repo", stage=14))]},
        None,
        run_fn=lambda m: None,
        handle_reply_fn=fake_reply,
        scheduler=scheduler,
        sns_publisher=sns,
    )
    # dormant carries no follow_up intent -> no further enqueue, but SNS fires.
    assert scheduler.calls == []
    assert len(sns.intents) == 1


# ---------------------------------------------------------------------------
# REQ-6.4 — SNS publish only when there is an intent
# ---------------------------------------------------------------------------


def test_no_sns_intent_means_no_publish(processor):
    sns = FakeSnsPublisher()
    result = OrchestrationResult(
        repo_full_name="owner/repo", status="skipped_complex", stage_reached="analyst"
    )
    outcomes = processor.perform_side_effects(
        result, scheduler=FakeScheduler(), sns_publisher=sns
    )
    assert sns.intents == []
    assert outcomes["sns_published"] is False
    assert outcomes["scheduled"] is None


# ---------------------------------------------------------------------------
# The SQS DelaySeconds > 900 caveat — honest handling
# ---------------------------------------------------------------------------


def test_scheduler_does_not_send_multi_day_delay(processor):
    """604800s exceeds the SQS 900s cap: build the message, flag it, do NOT send."""
    sqs = RecordingSqs()
    scheduler = processor.FollowUpScheduler(sqs_client=sqs, queue_url="https://sqs.test/q.fifo")
    result = scheduler.schedule(
        repo_full_name="owner/repo",
        delay_seconds=604800,
        stage=7,
        pr_number=42,
        issue_number=7,
        now=NOW,
    )
    assert result.scheduled is False
    assert result.exceeds_sqs_max is True
    assert result.mechanism == "eventbridge_scheduler_required"
    assert result.requested_delay_seconds == 604800
    assert result.fire_at == (NOW + timedelta(seconds=604800)).isoformat()
    # The message is still fully built and re-routable...
    assert result.message_group_id == "owner/repo"
    body = json.loads(result.body)
    assert body["message_type"] == "follow_up"
    assert body["stage"] == 7
    # ...but nothing was actually sent to SQS (an invalid/misleading send).
    assert sqs.sent == []


def test_scheduler_sends_within_sqs_cap(processor):
    """A genuine sub-900s delay is sent as-is with DelaySeconds."""
    sqs = RecordingSqs()
    scheduler = processor.FollowUpScheduler(sqs_client=sqs, queue_url="https://sqs.test/q.fifo")
    result = scheduler.schedule(
        repo_full_name="owner/repo", delay_seconds=300, stage=7, now=NOW
    )
    assert result.scheduled is True
    assert result.exceeds_sqs_max is False
    assert len(sqs.sent) == 1
    sent = sqs.sent[0]
    assert sent["DelaySeconds"] == 300
    assert sent["MessageGroupId"] == "owner/repo"
    assert sent["MessageDeduplicationId"] == result.dedup_id


def test_follow_up_dedup_id_distinguishes_stage(processor):
    seven = processor.build_follow_up_dedup_id("owner/repo", stage=7, now=NOW)
    fourteen = processor.build_follow_up_dedup_id("owner/repo", stage=14, now=NOW)
    same = processor.build_follow_up_dedup_id(
        "owner/repo", stage=7, now=NOW + timedelta(minutes=5)
    )
    assert seven != fourteen
    assert seven == same  # same (repo, stage) inside the window collapses
    assert len(seven) == 64


# ---------------------------------------------------------------------------
# Partial batch failure (design.md section 10)
# ---------------------------------------------------------------------------


def test_middle_record_failure_is_isolated(processor):
    def fake_run(msg):
        if msg["repo_full_name"] == "owner/boom":
            raise RuntimeError("kaboom")
        return OrchestrationResult(repo_full_name=msg["repo_full_name"], status="pr_opened", stage_reached="done")

    event = {
        "Records": [
            _record(_candidate_body("owner/a"), message_id="id-a"),
            _record(_candidate_body("owner/boom"), message_id="id-boom"),
            _record(_candidate_body("owner/c"), message_id="id-c"),
        ]
    }
    result = processor.lambda_handler(
        event,
        None,
        run_fn=fake_run,
        handle_reply_fn=lambda e: None,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
        note_writer=lambda *a, **k: None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "id-boom"}]}


# ---------------------------------------------------------------------------
# Error handling + best-effort note
# ---------------------------------------------------------------------------


def test_run_exception_reported_and_note_attempted(processor):
    notes = []

    def fake_run(msg):
        raise RuntimeError("pipeline blew up")

    def note_writer(repo, note, *, table_name=None):
        notes.append((repo, note))

    result = processor.lambda_handler(
        {"Records": [_record(_candidate_body("owner/repo"), message_id="x")]},
        None,
        run_fn=fake_run,
        handle_reply_fn=lambda e: None,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
        note_writer=note_writer,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "x"}]}
    assert len(notes) == 1
    assert notes[0][0] == "owner/repo"
    assert "RuntimeError" in notes[0][1]


def test_note_write_failure_does_not_mask_original(processor):
    def fake_run(msg):
        raise RuntimeError("original failure")

    def broken_note_writer(repo, note, *, table_name=None):
        raise RuntimeError("dynamo down")

    # Must not raise; the original failure is still reported as a batch failure.
    result = processor.lambda_handler(
        {"Records": [_record(_candidate_body("owner/repo"), message_id="x")]},
        None,
        run_fn=fake_run,
        handle_reply_fn=lambda e: None,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
        note_writer=broken_note_writer,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "x"}]}


# ---------------------------------------------------------------------------
# No-op results
# ---------------------------------------------------------------------------


def test_no_op_result_enqueues_and_publishes_nothing(processor):
    scheduler = FakeScheduler()
    sns = FakeSnsPublisher()

    def fake_reply(evt):
        # handle_reply on a repo that is not pr_opened: no follow_up, no sns.
        return OrchestrationResult(
            repo_full_name="owner/repo", status="success", stage_reached="follow_up"
        )

    result = processor.lambda_handler(
        {"Records": [_record(_follow_up_body("owner/repo", stage=7))]},
        None,
        run_fn=lambda m: None,
        handle_reply_fn=fake_reply,
        scheduler=scheduler,
        sns_publisher=sns,
    )
    assert scheduler.calls == []
    assert sns.intents == []
    assert result == {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# Malformed body
# ---------------------------------------------------------------------------


def test_malformed_json_body_is_batch_failure_not_crash(processor):
    event = {"Records": [{"messageId": "bad", "body": "{not json"}]}
    result = processor.lambda_handler(
        event,
        None,
        run_fn=lambda m: None,
        handle_reply_fn=lambda e: None,
        scheduler=FakeScheduler(),
        sns_publisher=FakeSnsPublisher(),
        note_writer=lambda *a, **k: None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_empty_batch_returns_no_failures(processor):
    assert processor.lambda_handler({"Records": []}, None) == {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# SNS publisher against moto
# ---------------------------------------------------------------------------


def test_sns_publisher_against_moto(processor):
    with mock_aws():
        sns = boto3.client("sns", region_name=REGION)
        topic_arn = sns.create_topic(Name="alerts")["TopicArn"]
        publisher = processor.SnsPublisher(sns_client=sns, topic_arn=topic_arn)
        response = publisher.publish({"subject": "hi", "message": "body"})
        assert "MessageId" in response


# ---------------------------------------------------------------------------
# Boundary: the Processor is not a second state writer (REQ-4.4)
# ---------------------------------------------------------------------------


def test_processor_never_calls_transition_or_write_state(processor):
    """AST assert: no dynamo_tools.transition / write_state in the Processor."""
    source = Path(processor.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {"transition", "write_state"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            value = node.value
            if isinstance(value, ast.Name) and value.id == "dynamo_tools":
                offenders.append(node.attr)
    assert offenders == []
