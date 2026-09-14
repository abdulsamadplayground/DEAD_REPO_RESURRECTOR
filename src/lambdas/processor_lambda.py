"""Processor Lambda — drive the Orchestrator and perform SQS/SNS side effects.

Triggered by the candidate **SQS FIFO** queue ``resurrector-candidates.fifo``
(design.md sections 2, 5, 9). Its job is *not* to re-implement the pipeline: the
Analyst → Engineer → Communicator work (REQ-3.*) and every DynamoDB state write
(REQ-4.4) live inside :func:`src.agents.orchestrator.run` /
:func:`~src.agents.orchestrator.handle_reply`. The Processor is the thin,
side-effecting shell around the Orchestrator that:

1. Parses each SQS record body and **routes** it (design.md section 5):
   - a *candidate* body (no ``message_type``, produced by
     :func:`src.lambdas.scanner_lambda.build_message_body`) → ``Orchestrator.run``.
   - a *follow-up timer* body (``message_type == "follow_up"``, ``stage`` 7 or
     14, produced by *this* Processor) → ``Orchestrator.handle_reply``.
   Maintainer webhook events do **not** arrive here — they go through the
   Webhook Lambda (Task 10). The Processor only ever sees candidates and the
   follow-up timers it enqueues itself.
2. **Performs** the AWS messaging the Orchestrator only *signals*
   (orchestrator module docstring): the 7-day follow-up enqueue (REQ-6.1), the
   14-day secondary enqueue (REQ-6.2), and the operator SNS alert (REQ-6.4). The
   Orchestrator returns these as :attr:`OrchestrationResult.follow_up` /
   :attr:`~OrchestrationResult.sns` intents; the Processor reads the intents off
   the result and acts on them. It never recomputes delays — it uses the value
   the result carries (sanity-checked against the orchestrator constants).
3. Reports **partial batch failures** so a single bad record is redriven (and
   ultimately dead-lettered after ``maxReceiveCount`` receives) without
   reprocessing the whole batch (design.md section 10).

Boundary — the Processor is not a second state writer (REQ-4.4)
---------------------------------------------------------------
Only the Orchestrator writes status transitions (design.md section 3): the
Processor never calls :func:`src.tools.dynamo_tools.transition` or
``write_state``. The one DynamoDB touch it *is* allowed is the best-effort
**diagnostic note** design.md section 10 sanctions ("Processor Lambda catches
unhandled exceptions, writes ``last_action_at`` + ``notes``"). That note is an
annotation, never a status change, is wrapped so a failed note-write can never
mask the original error, and is fully injectable for tests. Status ownership
stays with the Orchestrator.

The SQS ``DelaySeconds`` problem — the single most important decision here
------------------------------------------------------------------------------
REQ-6.1/6.2 ask for follow-up timers at ``DelaySeconds`` of ``604800`` (7 days)
and ``1209600`` (14 days). **SQS caps ``DelaySeconds`` at 900 seconds (15
minutes) on *both* standard and FIFO queues.** Passing ``604800`` to
``send_message`` is rejected at runtime (``InvalidParameterValue``); silently
capping it to 900 would fire the "7-day" nudge 15 minutes after the PR opens —
a correctness bug dressed up as working code. A pure SQS-``DelaySeconds``
approach therefore *cannot* implement multi-day timers.

The AWS-native primitive for a multi-day, one-shot timer is **EventBridge
Scheduler** (``create_schedule`` with a one-time ``at(...)`` expression), or a
DynamoDB TTL + stream. ``aws-constraints.md`` lists only services already in use
and grants this Lambda ``sqs:SendMessage`` (not EventBridge), so wiring
EventBridge is an infrastructure decision that belongs to the deploy task (Task
13), not code we can honestly ship and test today.

To stay honest **and** testable, the enqueue lives behind an injectable
:class:`FollowUpScheduler` seam. The default scheduler always *builds* the
follow-up message (correct ``MessageGroupId``, deterministic
``MessageDeduplicationId``, ``message_type``/``stage`` for re-routing) and
records the intended fire time, but it only calls ``send_message`` when the
requested delay is within the SQS 900s maximum. For the real 7/14-day delays it
does **not** send an invalid/misleading message: it flags the result
(``exceeds_sqs_max=True``, ``mechanism="eventbridge_scheduler_required"``) and
logs a prominent warning, so the gap is loud, recorded, and covered by a test —
never papered over. Production multi-day scheduling requires the EventBridge
Scheduler decision in Task 13; this cannot be validated offline.

Configuration (env vars; ``_env`` helpers per house style)
----------------------------------------------------------
- ``RESURRECTOR_CANDIDATE_QUEUE_URL`` — the FIFO queue follow-up timers are
  enqueued onto (same name the Scanner uses).
- ``RESURRECTOR_SNS_TOPIC_ARN`` — operator-alert topic for REQ-6.4 escalations.
- ``RESURRECTOR_FOLLOWUP_DEDUP_WINDOW_SECONDS`` — dedup time-bucket width for
  follow-up enqueues (default 86400, i.e. one day).
- ``RESURRECTOR_STATE_TABLE`` — DynamoDB table (read by ``dynamo_tools`` for the
  diagnostic note only).

Verification status
-------------------
Everything below is exercised offline with injected ``run``/``handle_reply``
callables, injected SQS/SNS clients (or moto), and an injected note writer. The
live pieces no offline test can cover — the Bedrock round-trip inside the
Orchestrator's agent, real GitHub/DynamoDB/SNS/SQS calls, and crucially the
**multi-day delay itself** — remain unverified without credentials and the
Task-13 EventBridge decision.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import boto3

from src.agents import orchestrator
from src.lambdas import scanner_lambda
from src.tools import dynamo_tools

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:  # pragma: no cover - Lambda configures the root logger
    LOGGER.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUEUE_URL_ENV_VAR = "RESURRECTOR_CANDIDATE_QUEUE_URL"
SNS_TOPIC_ARN_ENV_VAR = "RESURRECTOR_SNS_TOPIC_ARN"
FOLLOWUP_DEDUP_WINDOW_ENV_VAR = "RESURRECTOR_FOLLOWUP_DEDUP_WINDOW_SECONDS"

#: One day. Two identical follow-up enqueues for the same (repo, stage) inside
#: this window collapse to a single message via the FIFO dedup id.
DEFAULT_FOLLOWUP_DEDUP_WINDOW_SECONDS = 24 * 60 * 60

#: The hard SQS limit for ``DelaySeconds`` on BOTH standard and FIFO queues.
#: 7/14-day follow-ups (604800 / 1209600) blow past this — see the module
#: docstring for the full rationale and the EventBridge recommendation.
SQS_MAX_DELAY_SECONDS = 900

#: The message-type marker follow-up timers carry so redelivery re-routes to
#: ``Orchestrator.handle_reply``. Re-exported from the Orchestrator so there is
#: one source of truth.
FOLLOW_UP_MESSAGE_TYPE = orchestrator.FOLLOW_UP_MESSAGE_TYPE

#: The delays the Orchestrator is expected to signal, for a light sanity check.
KNOWN_FOLLOW_UP_DELAYS = frozenset(
    {orchestrator.FOLLOW_UP_7D_SECONDS, orchestrator.FOLLOW_UP_14D_SECONDS}
)


class ConfigurationError(RuntimeError):
    """Raised when required Lambda configuration is missing or invalid."""


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now() -> datetime:
    """Current UTC time (indirected so tests can inject a fixed clock)."""
    return datetime.now(timezone.utc)


def _queue_url(queue_url: Optional[str] = None) -> str:
    """Resolve the candidate queue URL, raising a clear error when unset."""
    resolved = queue_url or os.environ.get(QUEUE_URL_ENV_VAR)
    if not resolved:
        raise ConfigurationError(
            f"candidate queue URL is not configured; set {QUEUE_URL_ENV_VAR}"
        )
    return resolved


def _topic_arn(topic_arn: Optional[str] = None) -> str:
    """Resolve the operator-alert SNS topic ARN, raising when unset."""
    resolved = topic_arn or os.environ.get(SNS_TOPIC_ARN_ENV_VAR)
    if not resolved:
        raise ConfigurationError(
            f"SNS topic ARN is not configured; set {SNS_TOPIC_ARN_ENV_VAR}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Lazily-created AWS clients (built on first use so moto can intercept them).
# ---------------------------------------------------------------------------

_SQS_CLIENT: Any = None
_SNS_CLIENT: Any = None


def _sqs_client() -> Any:
    """Return the process-wide SQS client, creating it on first use."""
    global _SQS_CLIENT
    if _SQS_CLIENT is None:
        _SQS_CLIENT = boto3.client("sqs")
    return _SQS_CLIENT


def _sns_client() -> Any:
    """Return the process-wide SNS client, creating it on first use."""
    global _SNS_CLIENT
    if _SNS_CLIENT is None:
        _SNS_CLIENT = boto3.client("sns")
    return _SNS_CLIENT


def reset_clients() -> None:
    """Drop cached AWS clients (used by tests)."""
    global _SQS_CLIENT, _SNS_CLIENT
    _SQS_CLIENT = None
    _SNS_CLIENT = None


# ---------------------------------------------------------------------------
# Follow-up dedup id (REQ-6.1 / REQ-6.2)
# ---------------------------------------------------------------------------


def build_follow_up_dedup_id(
    repo_full_name: str,
    *,
    stage: int,
    now: datetime,
    window_seconds: Optional[int] = None,
) -> str:
    """Deterministic ``MessageDeduplicationId`` for a follow-up timer message.

    Reuses :func:`src.lambdas.scanner_lambda.build_deduplication_id` — the same
    ``sha256(key + ":" + time_bucket)`` scheme the Scanner uses — but composes
    the key from ``repo_full_name`` **and** ``stage``. The Scanner's key is
    ``repo`` alone, which is right for candidates but wrong here: a 7-day and a
    14-day enqueue for the same repo can legitimately fall in the same time
    bucket, and keying on ``repo`` alone would make the second collapse into the
    first. Folding ``stage`` into the key keeps the two distinct while still
    collapsing two *identical* (repo, stage) enqueues inside the window.

    The window defaults to one day (``DEFAULT_FOLLOWUP_DEDUP_WINDOW_SECONDS``),
    coarser than the Scanner's 6-hour window because follow-up timers are a
    day-scale concern.
    """
    window = (
        window_seconds
        if window_seconds is not None
        else _env_int(
            FOLLOWUP_DEDUP_WINDOW_ENV_VAR, DEFAULT_FOLLOWUP_DEDUP_WINDOW_SECONDS
        )
    )
    composite_key = f"{repo_full_name}:follow_up:{stage}"
    return scanner_lambda.build_deduplication_id(
        composite_key, now=now, window_seconds=window
    )


def build_follow_up_body(
    *,
    repo_full_name: str,
    stage: int,
    pr_number: Optional[int],
    issue_number: Optional[int],
    fire_at: str,
) -> str:
    """Serialize the follow-up timer body the Processor re-consumes.

    Carries ``message_type="follow_up"`` and ``stage`` so redelivery routes back
    into :func:`src.agents.orchestrator.handle_reply`. Keys are sorted so the
    body is deterministic for a given input.
    """
    payload = {
        "message_type": FOLLOW_UP_MESSAGE_TYPE,
        "stage": stage,
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "issue_number": issue_number,
        "fire_at": fire_at,
    }
    return json.dumps(payload, sort_keys=True)


# ---------------------------------------------------------------------------
# Follow-up scheduler seam (REQ-6.1 / REQ-6.2) — the SQS-DelaySeconds decision
# ---------------------------------------------------------------------------


@dataclass
class ScheduleResult:
    """Outcome of one :meth:`FollowUpScheduler.schedule` call.

    ``scheduled`` is ``True`` only when the message was actually placed on SQS
    with its real delay. For the 7/14-day timers the delay exceeds the SQS 900s
    maximum, so ``scheduled`` is ``False``, ``exceeds_sqs_max`` is ``True``, and
    ``mechanism`` names what production must use instead. The message is still
    fully *built* (``message_group_id`` / ``dedup_id`` / ``body``) and the
    intended ``fire_at`` recorded, so the intent is observable and testable.
    """

    scheduled: bool
    requested_delay_seconds: int
    fire_at: str
    exceeds_sqs_max: bool
    mechanism: str
    stage: int
    repo_full_name: str
    message_group_id: str
    dedup_id: str
    body: str
    message_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "scheduled": self.scheduled,
            "requested_delay_seconds": self.requested_delay_seconds,
            "fire_at": self.fire_at,
            "exceeds_sqs_max": self.exceeds_sqs_max,
            "mechanism": self.mechanism,
            "stage": self.stage,
            "repo_full_name": self.repo_full_name,
            "message_group_id": self.message_group_id,
            "dedup_id": self.dedup_id,
            "body": self.body,
            "message_id": self.message_id,
        }


class FollowUpScheduler:
    """Enqueue follow-up timers, honestly handling the SQS 900s delay cap.

    This is the seam that keeps the ``DelaySeconds`` decision honest (see the
    module docstring). :meth:`schedule` always builds the follow-up message, but
    only sends it via SQS when the requested delay is within
    :data:`SQS_MAX_DELAY_SECONDS`. For a real 7/14-day delay it refuses to send
    an invalid or misleadingly-short message, flags the result, and logs a
    warning pointing at the EventBridge Scheduler decision (Task 13).

    ``sqs_client`` and ``queue_url`` are injectable so tests never touch AWS.
    """

    def __init__(
        self,
        *,
        sqs_client: Any = None,
        queue_url: Optional[str] = None,
        window_seconds: Optional[int] = None,
    ) -> None:
        self._sqs_client = sqs_client
        self._queue_url = queue_url
        self._window_seconds = window_seconds

    def _client(self) -> Any:
        if self._sqs_client is not None:
            return self._sqs_client
        return _sqs_client()

    def schedule(
        self,
        *,
        repo_full_name: str,
        delay_seconds: int,
        stage: int,
        pr_number: Optional[int] = None,
        issue_number: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> ScheduleResult:
        """Build and (when possible) send one follow-up timer message.

        Returns a :class:`ScheduleResult` recording exactly what happened —
        including the honest ``exceeds_sqs_max`` flag when the delay is beyond
        the SQS maximum and the message therefore was *not* sent.
        """
        reference = now if now is not None else _now()
        fire_at = (reference + timedelta(seconds=delay_seconds)).isoformat()
        group_id = repo_full_name
        dedup_id = build_follow_up_dedup_id(
            repo_full_name,
            stage=stage,
            now=reference,
            window_seconds=self._window_seconds,
        )
        body = build_follow_up_body(
            repo_full_name=repo_full_name,
            stage=stage,
            pr_number=pr_number,
            issue_number=issue_number,
            fire_at=fire_at,
        )

        if delay_seconds > SQS_MAX_DELAY_SECONDS:
            # HONEST PATH: do not send. A send with DelaySeconds=604800 is
            # rejected by SQS, and capping to 900 would fire the "7-day" nudge
            # 15 minutes after the PR opens. Flag it loudly instead.
            LOGGER.warning(
                "follow-up for %s stage=%s requests delay=%ss which exceeds the "
                "SQS maximum of %ss; NOT enqueued. Multi-day scheduling requires "
                "EventBridge Scheduler (Task 13). Intended fire_at=%s.",
                repo_full_name,
                stage,
                delay_seconds,
                SQS_MAX_DELAY_SECONDS,
                fire_at,
            )
            return ScheduleResult(
                scheduled=False,
                requested_delay_seconds=delay_seconds,
                fire_at=fire_at,
                exceeds_sqs_max=True,
                mechanism="eventbridge_scheduler_required",
                stage=stage,
                repo_full_name=repo_full_name,
                message_group_id=group_id,
                dedup_id=dedup_id,
                body=body,
            )

        # Within the SQS cap: a genuine short delay can be sent as-is. (Not used
        # by the 7/14-day timers, but keeps the seam correct and exercisable.)
        resolved_url = _queue_url(self._queue_url)
        response = self._client().send_message(
            QueueUrl=resolved_url,
            MessageBody=body,
            MessageGroupId=group_id,
            MessageDeduplicationId=dedup_id,
            DelaySeconds=delay_seconds,
            MessageAttributes={
                "message_type": {
                    "DataType": "String",
                    "StringValue": FOLLOW_UP_MESSAGE_TYPE,
                },
                "stage": {"DataType": "Number", "StringValue": str(stage)},
            },
        )
        return ScheduleResult(
            scheduled=True,
            requested_delay_seconds=delay_seconds,
            fire_at=fire_at,
            exceeds_sqs_max=False,
            mechanism="sqs_delay",
            stage=stage,
            repo_full_name=repo_full_name,
            message_group_id=group_id,
            dedup_id=dedup_id,
            body=body,
            message_id=response.get("MessageId") if isinstance(response, dict) else None,
        )


# ---------------------------------------------------------------------------
# SNS publisher seam (REQ-6.4)
# ---------------------------------------------------------------------------


class SnsPublisher:
    """Publish operator-alert notifications for REQ-6.4 escalations.

    ``sns_client`` and ``topic_arn`` are injectable for offline testing.
    """

    def __init__(self, *, sns_client: Any = None, topic_arn: Optional[str] = None) -> None:
        self._sns_client = sns_client
        self._topic_arn = topic_arn

    def _client(self) -> Any:
        if self._sns_client is not None:
            return self._sns_client
        return _sns_client()

    def publish(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Publish one ``{subject, message}`` intent to the alerts topic."""
        resolved_arn = _topic_arn(self._topic_arn)
        subject = intent.get("subject") or "[Resurrector] escalation"
        message = intent.get("message") or json.dumps(intent, sort_keys=True)
        return self._client().publish(
            TopicArn=resolved_arn, Subject=subject, Message=message
        )


# ---------------------------------------------------------------------------
# Best-effort diagnostic note (design.md section 10)
# ---------------------------------------------------------------------------


def record_error_note(
    repo_full_name: str,
    note: str,
    *,
    table_name: Optional[str] = None,
) -> None:
    """Write ``notes`` + ``last_action_at`` for a repo WITHOUT a status change.

    design.md section 10 sanctions the Processor writing ``last_action_at`` +
    ``notes`` when it catches an unhandled exception. This is a diagnostic
    annotation only — it never changes ``status`` and never calls
    :func:`src.tools.dynamo_tools.transition` / ``write_state`` (those are the
    Orchestrator's, REQ-4.4). It updates just the two attributes via a direct
    ``update_item`` so the boundary holds. Callers invoke it best-effort; a
    failure here must not mask the original error (handled by the caller).
    """
    table = dynamo_tools._get_table(table_name)
    table.update_item(
        Key={"repo_full_name": repo_full_name},
        UpdateExpression="SET #n = :note, #la = :now",
        ExpressionAttributeNames={"#n": "notes", "#la": "last_action_at"},
        ExpressionAttributeValues={
            ":note": note,
            ":now": dynamo_tools._now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------


def _parse_body(record: dict[str, Any]) -> dict[str, Any]:
    """Parse an SQS record's JSON body into a dict.

    Raises ``ValueError`` for a missing/malformed/non-object body so the caller
    can report the record as a batch item failure (it will hit the DLQ after
    ``maxReceiveCount`` receives — design.md section 10).
    """
    raw = record.get("body")
    if not isinstance(raw, str) or raw.strip() == "":
        raise ValueError("SQS record has no string body")
    parsed = json.loads(raw)  # JSONDecodeError is a ValueError subclass
    if not isinstance(parsed, dict):
        raise ValueError("SQS record body is not a JSON object")
    return parsed


def _sanity_check_delay(delay_seconds: Any, repo: str, stage: Any) -> None:
    """Log if the Orchestrator signalled an unexpected delay (do not recompute)."""
    if delay_seconds not in KNOWN_FOLLOW_UP_DELAYS:
        LOGGER.warning(
            "follow-up intent for %s stage=%s carries unexpected delay=%s; "
            "using it as-is (delays are the Orchestrator's to decide)",
            repo,
            stage,
            delay_seconds,
        )


def perform_side_effects(
    result: orchestrator.OrchestrationResult,
    *,
    scheduler: FollowUpScheduler,
    sns_publisher: SnsPublisher,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Perform the SQS enqueue + SNS publish the Orchestrator signalled.

    Reads :attr:`OrchestrationResult.follow_up` (REQ-6.1/6.2) and
    :attr:`~OrchestrationResult.sns` (REQ-6.4) off ``result`` and acts on them.
    A ``None`` intent means "nothing to do" — no enqueue / no publish. Returns a
    JSON-serializable summary of what was performed.
    """
    outcomes: dict[str, Any] = {"scheduled": None, "sns_published": False}

    follow_up = result.follow_up
    if follow_up:
        delay = follow_up.get("delay_seconds")
        stage = int(follow_up.get("stage", 7))
        _sanity_check_delay(delay, result.repo_full_name, stage)
        schedule_result = scheduler.schedule(
            repo_full_name=result.repo_full_name,
            delay_seconds=int(delay),
            stage=stage,
            pr_number=result.pr_number,
            issue_number=result.issue_number,
            now=now,
        )
        outcomes["scheduled"] = schedule_result.to_dict()

    if result.sns:
        sns_publisher.publish(result.sns)
        outcomes["sns_published"] = True

    return outcomes


def process_record(
    record: dict[str, Any],
    *,
    run_fn: Callable[[dict[str, Any]], orchestrator.OrchestrationResult],
    handle_reply_fn: Callable[[dict[str, Any]], orchestrator.OrchestrationResult],
    scheduler: FollowUpScheduler,
    sns_publisher: SnsPublisher,
    now: Optional[datetime] = None,
) -> orchestrator.OrchestrationResult:
    """Process one SQS record: parse → route → run Orchestrator → side effects.

    Routing (design.md section 5): a body carrying ``message_type == "follow_up"``
    is a fired timer → ``handle_reply``; anything else is a candidate → ``run``.
    Any exception propagates to :func:`lambda_handler`, which records it as a
    batch item failure.
    """
    body = _parse_body(record)
    if body.get("message_type") == FOLLOW_UP_MESSAGE_TYPE:
        result = handle_reply_fn(body)
    else:
        result = run_fn(body)
    perform_side_effects(
        result, scheduler=scheduler, sns_publisher=sns_publisher, now=now
    )
    return result


def _best_effort_note(
    record: dict[str, Any],
    error: BaseException,
    *,
    note_writer: Callable[..., None],
    table_name: Optional[str],
) -> None:
    """Try to annotate the repo's record with the failure; never raise.

    A best-effort implementation of design.md section 10. If the body cannot be
    parsed (so we do not know which repo) or the note write itself fails, we log
    and move on — the original failure is already being reported as a batch item
    failure and must not be masked.
    """
    try:
        body = _parse_body(record)
    except ValueError:
        LOGGER.warning("cannot record error note: record body is unparseable")
        return
    repo = body.get("repo_full_name")
    if not repo:
        LOGGER.warning("cannot record error note: body has no repo_full_name")
        return
    note = f"processor error ({error.__class__.__name__}): {error}"
    try:
        note_writer(repo, note, table_name=table_name)
    except Exception:  # noqa: BLE001 - a failed note must not mask the original
        LOGGER.exception("best-effort error note failed for %s", repo)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(
    event: dict[str, Any],
    context: Any = None,  # noqa: ARG001
    *,
    run_fn: Optional[Callable[[dict[str, Any]], orchestrator.OrchestrationResult]] = None,
    handle_reply_fn: Optional[
        Callable[[dict[str, Any]], orchestrator.OrchestrationResult]
    ] = None,
    scheduler: Optional[FollowUpScheduler] = None,
    sns_publisher: Optional[SnsPublisher] = None,
    note_writer: Optional[Callable[..., None]] = None,
    table_name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """SQS FIFO entry point with partial-batch-failure reporting.

    ``event`` is an SQS batch (``event["Records"]``). Each record is processed
    independently: on success its follow-up/SNS intents are performed; on any
    exception it is logged, a best-effort diagnostic note is attempted, and its
    ``messageId`` is added to ``batchItemFailures`` so only that record is
    redriven (and dead-lettered after ``maxReceiveCount`` receives — design.md
    section 10). This requires ``ReportBatchItemFailures`` on the event source
    mapping (set in ``template.yaml``).

    Every dependency is injectable so the whole handler runs offline with no AWS
    and no model calls. Defaults wire the real Orchestrator functions and lazy
    AWS-backed scheduler / publisher / note writer.
    """
    _run = run_fn or orchestrator.run
    _handle_reply = handle_reply_fn or orchestrator.handle_reply
    _scheduler = scheduler if scheduler is not None else FollowUpScheduler()
    _sns_publisher = sns_publisher if sns_publisher is not None else SnsPublisher()
    _note_writer = note_writer or record_error_note

    records = event.get("Records", []) if isinstance(event, dict) else []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId")
        try:
            result = process_record(
                record,
                run_fn=_run,
                handle_reply_fn=_handle_reply,
                scheduler=_scheduler,
                sns_publisher=_sns_publisher,
                now=now,
            )
            LOGGER.info(
                "processed %s -> status=%s stage=%s",
                result.repo_full_name,
                result.status,
                result.stage_reached,
            )
        except Exception as exc:  # noqa: BLE001 - one bad record must not fail the batch
            LOGGER.exception("failed to process record %s", message_id)
            _best_effort_note(
                record, exc, note_writer=_note_writer, table_name=table_name
            )
            if message_id is not None:
                failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
