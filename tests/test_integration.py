"""Offline end-to-end integration tests (Task 12).

These exercise the pieces *together* across module boundaries, driving the real
production functions with only the two outermost seams faked:

- **AWS**: real DynamoDB via **moto** (``mock_aws``) — the actual
  ``ResurrectorState`` table (PK ``repo_full_name``, PAY_PER_REQUEST) is created
  and ``dynamo_tools`` reads/writes it for real, so every state transition is
  asserted against a real (mocked) table, not a spy. SQS/SNS in the Processor are
  injected recorders.
- **GitHub**: one rich fake ``Repository`` (``tests/_integration_fakes.FakeRepo``)
  threaded through the real ``github_tools`` / ``github_write`` / ``github_comms``
  functions via their ``client=`` seam. No network.
- **Model (Strands/Bedrock)**: a single deterministic ``run_model`` injected into
  the real ``analyze_repo`` / ``implement_fix`` / ``reply_to_maintainer`` code,
  exercising the real parsing / gating / pipeline without Bedrock.

Real-asset validation (real AWS deploy, real GitHub PRs, real Bedrock) is Task 13
and is deliberately out of scope here — nothing below makes a network/AWS/GitHub
call.

Flows covered (see each section):
  A  candidate -> pr_opened (REQ-2/3/4, REQ-6.1) + PR content quality
  B  complexity gate -> skipped_complex (REQ-2.3)
  C  engineer failure -> fix_failed (REQ-3.3)
  D  maintainer reply paths merged/closed/question (REQ-5.2/5.3/5.4) + webhook->handle_reply
  E  follow-up escalation 7d/14d timers (REQ-6.2/6.3/6.4) + Processor side effects
  F  Scanner -> queue -> Processor -> Orchestrator message-contract wiring
  plus a wiring assertion that all four Strands agents build together.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from src.agents import orchestrator
from src.lambdas import processor_lambda, scanner_lambda, webhook_lambda
from src.tools import dynamo_tools, pr_templates
from src.tools.github_search import RepoCandidate
from tests._integration_fakes import (
    FakeGithubClient,
    FakeIssue,
    FakeRepo,
    RecordingSns,
    RecordingSqs,
    make_model,
)

REPO = "owner/repo"
TABLE = "IntegResurrectorState"
REGION = "us-east-1"
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_table(monkeypatch):
    """A real (moto) ResurrectorState table; yields its name.

    dynamo_tools builds its boto3 resource on every call and reads the table
    name at call time, so no module reload is needed — creating the table inside
    the mock and passing ``table_name=TABLE`` to the pipeline is enough.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("RESURRECTOR_STATE_TABLE", TABLE)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "repo_full_name", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "repo_full_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield TABLE


def _bug_issue(number=42):
    return FakeIssue(
        number,
        title="Crash on empty config",
        body=(
            "Steps to reproduce:\n"
            "1. Call load({}) with an empty config\n"
            "It raises in loader.py line 12 with a KeyError. Expected: return {}.\n"
        ),
        thumbs_up=7,
        total_reactions=9,
        comments=3,
        labels=("bug",),
    )


def _repo_with_bug(issue=None):
    return FakeRepo(issues=[issue or _bug_issue()])


def _recording_transition(statuses):
    """Wrap the REAL dynamo transition so the order is observable AND persisted."""

    def _t(repo, status, *, table_name=None, **updates):
        statuses.append(status)
        return dynamo_tools.transition(repo, status, table_name=table_name, **updates)

    return _t


# ===========================================================================
# Flow A — happy path: candidate -> pr_opened
# ===========================================================================


def test_flow_a_candidate_to_pr_opened_full_pipeline(state_table):
    """REQ-2/3/4 + REQ-6.1: the whole chain runs against real DynamoDB + fake GitHub."""
    repo = _repo_with_bug()
    client = FakeGithubClient(repo)
    statuses: list[str] = []

    # The Scanner would have written `discovered`; mirror that so the row shows
    # the full discovered -> in_progress -> pr_opened lifecycle.
    dynamo_tools.write_state(
        dynamo_tools.RepoState(repo_full_name=REPO, status="discovered"),
        table_name=TABLE,
    )

    result = orchestrator.run(
        {"repo_full_name": REPO, "default_branch": "main"},
        client=client,
        run_model=make_model(),
        transition=_recording_transition(statuses),
        table_name=TABLE,
    )

    # -- DynamoDB transitions (real moto row) --------------------------------
    assert statuses == ["in_progress", "pr_opened"]
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row is not None
    assert row.status == "pr_opened"          # REQ-4.4 / REQ-7.1
    assert row.pr_number == 321
    assert row.pr_url == "https://github.com/owner/repo/pull/321"
    assert row.issue_number == 42
    assert row.opened_at is not None
    assert row.last_action_at is not None
    assert row.maintainer_responded is False
    assert row.follow_up_count == 0

    # -- the fake GitHub repo actually received the artifacts ----------------
    assert repo.branch_exists("resurrector/fix-issue-42")   # REQ-3.1
    assert len(repo.created_commits) == 1                   # REQ-3.4 (one atomic push)
    assert len(repo.created_pulls) == 1                     # REQ-4.1
    assert result.status == "pr_opened"
    assert result.stage_reached == "done"

    # -- 7-day follow-up intent signalled, not performed (REQ-6.1) -----------
    assert result.follow_up == {
        "delay_seconds": 604800,
        "message_type": "follow_up",
        "stage": 7,
    }


def test_flow_a_pr_content_quality(state_table):
    """REQ-4.1/4.2/4.3 + tone: exact title, four ordered sections, no banned words."""
    repo = _repo_with_bug()
    orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(),
        table_name=TABLE,
    )

    pull = repo.created_pulls[0]
    title = pull["title"]
    body = pull["body"]
    issue_comment = repo.comments_on(42)

    # REQ-4.1: exact title.
    assert title == "[Resurrector] Fix: Crash on empty config (closes #42)"

    # REQ-4.2: the four sections, in order, with closes #N and the co-maintenance note.
    positions = [body.find(h) for h in pr_templates.PR_BODY_SECTION_HEADINGS]
    assert all(p != -1 for p in positions), "every section heading must be present"
    assert positions == sorted(positions), "sections must be in the required order"
    assert "closes #42" in body
    assert pr_templates.REDUCED_MAINTENANCE_PHRASE in body

    # REQ-4.3: the issue comment links the PR.
    assert len(issue_comment) == 1
    assert "https://github.com/owner/repo/pull/321" in issue_comment[0]

    # Tone guard: no banned word anywhere a maintainer reads.
    haystack = (title + "\n" + body + "\n" + issue_comment[0]).lower()
    for banned in pr_templates.FORBIDDEN_TERMS:
        assert banned not in haystack


def test_flow_a_follow_up_intent_reaches_processor_scheduler(state_table):
    """REQ-6.1: the run result's 7-day intent, fed to the Processor, is scheduled
    honestly — 604800s exceeds the SQS 900s cap, so it is recorded but not sent."""
    repo = _repo_with_bug()
    result = orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(),
        table_name=TABLE,
    )

    sqs = RecordingSqs()
    sns = RecordingSns()
    scheduler = processor_lambda.FollowUpScheduler(
        sqs_client=sqs, queue_url="https://sqs.test/q.fifo"
    )
    publisher = processor_lambda.SnsPublisher(
        sns_client=sns, topic_arn="arn:aws:sns:us-east-1:1:alerts"
    )
    outcomes = processor_lambda.perform_side_effects(
        result, scheduler=scheduler, sns_publisher=publisher, now=NOW
    )

    scheduled = outcomes["scheduled"]
    assert scheduled["stage"] == 7
    assert scheduled["requested_delay_seconds"] == 604800
    assert scheduled["scheduled"] is False           # honest scheduler: not sent
    assert scheduled["exceeds_sqs_max"] is True
    assert scheduled["mechanism"] == "eventbridge_scheduler_required"
    assert sqs.sent == []                            # nothing put on the queue
    assert outcomes["sns_published"] is False        # run() carries no SNS intent


# ===========================================================================
# Flow B — complexity gate: candidate -> skipped_complex (REQ-2.3)
# ===========================================================================


def test_flow_b_complex_issue_is_skipped_engineer_never_runs(state_table):
    repo = _repo_with_bug()
    statuses: list[str] = []

    result = orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(
            analyst_json={
                "complexity": "complex",
                "confidence": 0.9,
                "files_affected": [],
                "approach": "Needs a redesign; ask the maintainers for scope.",
            }
        ),
        transition=_recording_transition(statuses),
        table_name=TABLE,
    )

    assert statuses == ["in_progress", "skipped_complex"]
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "skipped_complex"
    assert result.stage_reached == "analyst"
    # Engineer never ran: no branch, no commit; Communicator never ran: no PR.
    assert repo.created_refs == []
    assert repo.created_commits == []
    assert repo.created_pulls == []


def test_flow_b_low_confidence_is_skipped(state_table):
    repo = _repo_with_bug()
    result = orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(
            analyst_json={
                "complexity": "trivial",
                "confidence": 0.2,   # below the 0.6 default threshold
                "files_affected": ["loader.py"],
                "approach": "Guard the empty case.",
            }
        ),
        table_name=TABLE,
    )
    assert result.status == "skipped_complex"
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "skipped_complex"
    assert repo.created_pulls == []


# ===========================================================================
# Flow C — engineer failure: candidate -> fix_failed (REQ-3.3)
# ===========================================================================


def test_flow_c_unconfident_model_yields_fix_failed_no_pr(state_table):
    """REQ-3.3: an unconfident change set aborts before the branch; no PR opened."""
    repo = _repo_with_bug()
    statuses: list[str] = []

    result = orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(
            engineer_json={
                "summary": "Not sure how to fix this safely.",
                "commit_message": "",
                "files": [{"path": "loader.py", "content": "x = 1\n"}],
                "confident": False,
            }
        ),
        transition=_recording_transition(statuses),
        table_name=TABLE,
    )

    assert statuses == ["in_progress", "fix_failed"]
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "fix_failed"
    assert result.stage_reached == "engineer"
    # Branch-then-abort contract: an unconfident/invalid plan aborts BEFORE the
    # branch is created, and nothing downstream ran.
    assert repo.created_refs == []
    assert repo.created_commits == []
    assert repo.created_pulls == []
    assert repo.comments_on(42) == []


def test_flow_c_invalid_python_syntax_yields_fix_failed(state_table):
    """REQ-3.3: a syntactically invalid fix is rejected; no branch, no PR."""
    repo = _repo_with_bug()
    result = orchestrator.run(
        {"repo_full_name": REPO},
        client=FakeGithubClient(repo),
        run_model=make_model(
            engineer_json={
                "summary": "Guard the empty case.",
                "commit_message": "Fix",
                "files": [{"path": "loader.py", "content": "def load(:\n  return\n"}],
                "confident": True,
            }
        ),
        table_name=TABLE,
    )
    assert result.status == "fix_failed"
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "fix_failed"
    assert repo.created_refs == []       # syntax check aborts before branch creation
    assert repo.created_pulls == []


# ===========================================================================
# Flow D — maintainer reply paths (REQ-5.*) via real classify + real state
# ===========================================================================


def _seed_pr_opened(*, maintainer_responded=False, follow_up_count=0):
    dynamo_tools.write_state(
        dynamo_tools.RepoState(
            repo_full_name=REPO,
            status="pr_opened",
            issue_number=42,
            pr_number=321,
            pr_url="https://github.com/owner/repo/pull/321",
            opened_at="2024-05-01T00:00:00+00:00",
            maintainer_responded=maintainer_responded,
            follow_up_count=follow_up_count,
        ),
        table_name=TABLE,
    )


def test_flow_d_merged_sets_success(state_table):
    _seed_pr_opened()
    result = orchestrator.handle_reply(
        {
            "repo_full_name": REPO,
            "pr_number": 321,
            "merged": True,
            "action": "closed",
            "state": "closed",
            "is_maintainer": True,
        },
        table_name=TABLE,
    )
    assert result.status == "success"                       # REQ-5.2
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "success"
    assert row.maintainer_responded is True


def test_flow_d_closed_unmerged_sets_rejected(state_table):
    _seed_pr_opened()
    result = orchestrator.handle_reply(
        {
            "repo_full_name": REPO,
            "pr_number": 321,
            "merged": False,
            "action": "closed",
            "state": "closed",
            "is_maintainer": True,
        },
        table_name=TABLE,
    )
    assert result.status == "rejected"                      # REQ-5.4
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "rejected"


def test_flow_d_question_posts_reply_and_stays_open(state_table):
    _seed_pr_opened()
    repo = FakeRepo(issues=[_bug_issue()])
    result = orchestrator.handle_reply(
        {
            "repo_full_name": REPO,
            "pr_number": 321,
            "text": "Why not keep the previous default here?",
            "is_maintainer": True,
        },
        client=FakeGithubClient(repo),
        run_model=make_model(),
        table_name=TABLE,
    )
    assert result.status == "pr_opened"                     # REQ-5.3: stays open
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "pr_opened"
    assert row.maintainer_responded is True
    # The Communicator posted a reply on the PR conversation.
    pr_comments = repo.comments_on(321)
    assert len(pr_comments) == 1
    for banned in pr_templates.FORBIDDEN_TERMS:
        assert banned not in pr_comments[0].lower()


def test_flow_d_webhook_lambda_end_to_end_signature_to_handle_reply(state_table):
    """REQ-5.1: a signed webhook reaches handle_reply (which writes real state);
    a bad signature is 401 and never reaches it."""
    secret = "s3cr3t"
    payload = {
        "action": "closed",
        "repository": {"full_name": REPO},
        "pull_request": {"number": 321, "state": "closed", "merged": True},
    }
    raw = json.dumps(payload).encode("utf-8")
    good_sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    # Bad signature: rejected 401, handle_reply never called (no state change).
    _seed_pr_opened()
    reached = []
    bad_event = {
        "headers": {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }
    bad = webhook_lambda.lambda_handler(
        bad_event,
        handle_reply_fn=lambda event, **kw: reached.append(event),
        secret_loader=lambda: secret,
        token_loader=lambda: None,
        table_name=TABLE,
    )
    assert bad["statusCode"] == 401
    assert reached == []
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "pr_opened"

    # Good signature: reaches the REAL handle_reply -> real state write (success).
    good_event = {
        "headers": {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": good_sig},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }
    ok = webhook_lambda.lambda_handler(
        good_event,
        secret_loader=lambda: secret,
        token_loader=lambda: None,
        table_name=TABLE,
    )
    assert ok["statusCode"] == 200
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "success"


# ===========================================================================
# Flow E — follow-up escalation (REQ-6.2/6.3/6.4): the timer lifecycle
# ===========================================================================


def test_flow_e_seven_day_follow_up_posts_increments_and_signals(state_table):
    _seed_pr_opened()
    repo = FakeRepo(issues=[_bug_issue()])
    result = orchestrator.handle_reply(
        {"repo_full_name": REPO, "message_type": "follow_up", "stage": 7},
        client=FakeGithubClient(repo),
        table_name=TABLE,
    )

    # REQ-6.2: one warm follow-up comment on the PR.
    assert repo.comments_on(321) == [pr_templates.follow_up_comment(7)]
    # REQ-6.4: follow_up_count incremented in the real moto row.
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.follow_up_count == 1
    assert row.status == "pr_opened"
    # 14-day secondary intent + an SNS escalation intent are signalled.
    assert result.follow_up == {
        "delay_seconds": 1209600,
        "message_type": "follow_up",
        "stage": 14,
    }
    assert result.sns is not None

    # Feed the result to the Processor: SNS publishes, 14-day timer scheduled
    # (honestly not sent, exceeds the SQS cap).
    sqs, sns = RecordingSqs(), RecordingSns()
    outcomes = processor_lambda.perform_side_effects(
        result,
        scheduler=processor_lambda.FollowUpScheduler(
            sqs_client=sqs, queue_url="https://sqs.test/q.fifo"
        ),
        sns_publisher=processor_lambda.SnsPublisher(
            sns_client=sns, topic_arn="arn:aws:sns:us-east-1:1:alerts"
        ),
        now=NOW,
    )
    assert outcomes["sns_published"] is True
    assert len(sns.published) == 1
    assert outcomes["scheduled"]["stage"] == 14
    assert outcomes["scheduled"]["exceeds_sqs_max"] is True
    assert sqs.sent == []


def test_flow_e_fourteen_day_marks_dormant(state_table):
    _seed_pr_opened(follow_up_count=1)
    repo = FakeRepo(issues=[_bug_issue()])
    result = orchestrator.handle_reply(
        {"repo_full_name": REPO, "message_type": "follow_up", "stage": 14},
        client=FakeGithubClient(repo),
        table_name=TABLE,
    )
    assert result.status == "dormant"                       # REQ-6.3
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "dormant"
    assert row.follow_up_count == 2                          # REQ-6.4 (incremented again)
    assert result.sns is not None
    assert result.follow_up is None                          # stop follow-up
    assert repo.comments_on(321) == []                       # no comment at 14 days


def test_flow_e_seven_day_noop_when_maintainer_responded(state_table):
    _seed_pr_opened(maintainer_responded=True, follow_up_count=0)
    repo = FakeRepo(issues=[_bug_issue()])
    result = orchestrator.handle_reply(
        {"repo_full_name": REPO, "message_type": "follow_up", "stage": 7},
        client=FakeGithubClient(repo),
        table_name=TABLE,
    )
    assert "no-op" in result.notes
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.follow_up_count == 0                          # not incremented
    assert row.status == "pr_opened"                         # unchanged
    assert repo.comments_on(321) == []                       # no comment posted


def test_flow_e_escalation_is_bounded_no_repeat_after_dormant(state_table):
    """Single-nudge discipline: once the repo leaves pr_opened (dormant), a
    further follow-up timer is a no-op — escalation never repeats (REQ-6)."""
    dynamo_tools.write_state(
        dynamo_tools.RepoState(
            repo_full_name=REPO, status="dormant", pr_number=321, follow_up_count=2
        ),
        table_name=TABLE,
    )
    repo = FakeRepo(issues=[_bug_issue()])
    result = orchestrator.handle_reply(
        {"repo_full_name": REPO, "message_type": "follow_up", "stage": 7},
        client=FakeGithubClient(repo),
        table_name=TABLE,
    )
    assert "no-op" in result.notes
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.follow_up_count == 2                          # untouched
    assert row.status == "dormant"
    assert repo.comments_on(321) == []


# ===========================================================================
# Flow F — Scanner -> queue -> Processor -> Orchestrator message contract
# ===========================================================================


def test_flow_f_scanner_message_body_feeds_processor_and_run(state_table):
    """The exact body the Scanner enqueues must be what run() consumes. This is
    the test that catches a build_message_body <-> run() contract mismatch."""
    sqs = RecordingSqs()
    old_commit = NOW - timedelta(days=400)
    candidate = RepoCandidate(
        repo_full_name=REPO,
        stars=120,
        open_issues=25,
        last_commit_at=old_commit,
        default_branch="main",
        language="Python",
        html_url="https://github.com/owner/repo",
    )

    # 1. Scanner: search -> dedup (real moto, empty -> eligible) -> enqueue.
    summary = scanner_lambda.scan(
        github_token="unused-in-tests",
        sqs_client=sqs,
        queue_url="https://sqs.test/candidates.fifo",
        table_name=TABLE,
        search=lambda **kwargs: [candidate],
        now=NOW,
    )
    assert summary["enqueued"] == 1
    assert dynamo_tools.read_state(REPO, table_name=TABLE).status == "discovered"
    assert len(sqs.sent) == 1
    enqueued_body = sqs.sent[0]["MessageBody"]
    assert sqs.sent[0]["MessageGroupId"] == REPO            # REQ-1.5

    # 2. Feed that EXACT body into the Processor, routed to the real Orchestrator.
    repo = _repo_with_bug()
    client = FakeGithubClient(repo)
    event = {"Records": [{"messageId": "m1", "body": enqueued_body}]}
    response = processor_lambda.lambda_handler(
        event,
        run_fn=lambda body: orchestrator.run(
            body, client=client, run_model=make_model(), table_name=TABLE
        ),
        scheduler=processor_lambda.FollowUpScheduler(
            sqs_client=RecordingSqs(), queue_url="https://sqs.test/candidates.fifo"
        ),
        sns_publisher=processor_lambda.SnsPublisher(
            sns_client=RecordingSns(), topic_arn="arn:aws:sns:us-east-1:1:alerts"
        ),
        table_name=TABLE,
    )

    # No batch item failures: the contract lined up and the pipeline progressed.
    assert response["batchItemFailures"] == []
    row = dynamo_tools.read_state(REPO, table_name=TABLE)
    assert row.status == "pr_opened"
    assert row.pr_number == 321
    assert repo.created_pulls and repo.branch_exists("resurrector/fix-issue-42")


# ===========================================================================
# Wiring — all four Strands agents build together (strands IS installed)
# ===========================================================================


def test_all_four_agents_build_and_wire_together():
    """An integration assertion that the whole agent topology constructs (design
    §3/§8) — without invoking the live model."""
    from strands import Agent

    from src.agents.analyst import build_analyst
    from src.agents.communicator import build_communicator
    from src.agents.engineer import build_engineer

    analyst = build_analyst()
    engineer = build_engineer()
    communicator = build_communicator()
    orch = orchestrator.build_orchestrator()

    assert all(isinstance(a, Agent) for a in (analyst, engineer, communicator, orch))
    # The Orchestrator wires exactly the five design.md §3 tools.
    assert len(orchestrator.ORCHESTRATOR_TOOLS) == 5
