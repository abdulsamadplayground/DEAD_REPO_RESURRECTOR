"""Scanner Lambda — scheduled discovery, de-duplication, and FIFO enqueue.

Triggered by a CloudWatch cron rule every 6 hours (REQ-1.1). Its job is
"search -> dedup -> enqueue" (design.md section 9):

1. Run the distress-pattern GitHub search via :mod:`src.tools.github_search`,
   which already implements REQ-1.1 and REQ-1.2 (open-issue floor, last-commit
   age, at most 20 results per run).
2. For every candidate, read the ``ResurrectorState`` DynamoDB record and decide
   eligibility (REQ-1.3, REQ-1.4) with :func:`is_eligible`.
3. Enqueue each eligible candidate onto the candidate SQS **FIFO** queue with
   ``MessageGroupId = repo_full_name`` and a deterministic, content-derived
   ``MessageDeduplicationId`` (REQ-1.5).
4. Record ``status = discovered`` so the next scan dedups correctly and
   ``last_action_at`` is refreshed (REQ-7.1).

Interpretations of the requirements, made explicit
--------------------------------------------------

*"in active follow-up" (REQ-1.3).* Timed escalation (REQ-6) only ever runs while
a repo sits in ``pr_opened`` — the follow-up timers are queued when the PR is
opened and stop when the status becomes ``success`` / ``rejected`` / ``dormant``.
So "in active follow-up" is a strict subset of ``status == "pr_opened"``, and
blocking :data:`ACTIVE_STATUSES` (``in_progress`` and ``pr_opened``) is
sufficient to satisfy REQ-1.3 regardless of ``follow_up_count`` /
``maintainer_responded``.

*The ``closed`` status in REQ-1.4.* ``dynamo_tools.VALID_STATUSES`` has no
``closed`` member — design.md section 4 models a maintainer closing the PR as
``rejected``, and an operator-suppressed repo as ``ignored``. We therefore read
REQ-1.4's "``closed`` or ``ignored``" as "``rejected`` or ``ignored``" and, more
generally, apply the same age gate to every non-active status. No new status is
added to :mod:`src.tools.dynamo_tools`.

*``discovered`` is treated as in-flight.* A repo whose record says ``discovered``
has already been enqueued but not yet picked up by the Processor. Re-enqueueing
it would duplicate work, so it is gated by the same re-evaluation window; once
the window elapses we assume the message was lost and allow a retry.

*Boundary semantics.* REQ-1.4 says "more than 30 days ago", so a record whose
``last_action_at`` is *exactly* the window old is **not** yet eligible; it
becomes eligible strictly after the window.

Ordering / failure mode
-----------------------

We enqueue **first**, then write ``discovered`` state. If the state write fails
after a successful send, the next scan may enqueue the repo again — SQS FIFO
content-based dedup absorbs a repeat inside the dedup window, and the Processor
is idempotent enough to re-analyze a repo. The alternative order (state first)
would risk marking a repo ``discovered`` that was never enqueued, silently
dropping it for the whole re-evaluation window. We prefer a possible duplicate
over dropped work.

Configuration
-------------

- ``RESURRECTOR_CANDIDATE_QUEUE_URL`` — candidate FIFO queue URL (required).
- ``RESURRECTOR_STATE_TABLE`` — DynamoDB table (read by ``dynamo_tools``).
- ``RESURRECTOR_REEVALUATION_DAYS`` — re-evaluation window in days (default 30).
- ``RESURRECTOR_DEDUP_WINDOW_SECONDS`` — dedup time-bucket width (default 21600,
  i.e. one 6-hour scan window).
- ``RESURRECTOR_GITHUB_TOKEN_SECRET_ID`` — Secrets Manager id for the GitHub PAT
  (default ``/resurrector/github-token``).
- ``GITHUB_TOKEN`` — local-testing fallback, checked before Secrets Manager.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import boto3

from src.tools import dynamo_tools
from src.tools.dynamo_tools import RepoState
from src.tools.github_search import RepoCandidate, search_candidates

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:  # pragma: no cover - Lambda configures the root logger
    LOGGER.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUEUE_URL_ENV_VAR = "RESURRECTOR_CANDIDATE_QUEUE_URL"
REEVALUATION_DAYS_ENV_VAR = "RESURRECTOR_REEVALUATION_DAYS"
DEDUP_WINDOW_ENV_VAR = "RESURRECTOR_DEDUP_WINDOW_SECONDS"
GITHUB_TOKEN_SECRET_ID_ENV_VAR = "RESURRECTOR_GITHUB_TOKEN_SECRET_ID"
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"

DEFAULT_REEVALUATION_DAYS = 30
DEFAULT_DEDUP_WINDOW_SECONDS = 6 * 60 * 60  # one scan interval
DEFAULT_GITHUB_TOKEN_SECRET_ID = "/resurrector/github-token"

#: Statuses that mean the repo is currently being engaged. Never re-enqueue
#: these (REQ-1.3). ``pr_opened`` also covers "in active follow-up".
ACTIVE_STATUSES = frozenset({"in_progress", "pr_opened"})


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


# ---------------------------------------------------------------------------
# Lazily-created AWS clients
#
# Built on first use rather than at import time so that moto (and any other
# interceptor installed after import) can capture them.
# ---------------------------------------------------------------------------

_SQS_CLIENT: Any = None
_SECRETS_CLIENT: Any = None
_GITHUB_TOKEN_CACHE: Optional[str] = None


def _sqs_client() -> Any:
    """Return the process-wide SQS client, creating it on first use."""
    global _SQS_CLIENT
    if _SQS_CLIENT is None:
        _SQS_CLIENT = boto3.client("sqs")
    return _SQS_CLIENT


def _secrets_client() -> Any:
    """Return the process-wide Secrets Manager client, created on first use."""
    global _SECRETS_CLIENT
    if _SECRETS_CLIENT is None:
        _SECRETS_CLIENT = boto3.client("secretsmanager")
    return _SECRETS_CLIENT


def reset_clients() -> None:
    """Drop cached AWS clients and the cached token (used by tests)."""
    global _SQS_CLIENT, _SECRETS_CLIENT, _GITHUB_TOKEN_CACHE
    _SQS_CLIENT = None
    _SECRETS_CLIENT = None
    _GITHUB_TOKEN_CACHE = None


def load_github_token(
    *,
    secret_id: Optional[str] = None,
    secrets_client: Any = None,
    use_cache: bool = True,
) -> str:
    """Load the GitHub PAT, caching it for the life of the container.

    Resolution order (design.md sections 6 and 11): the ``GITHUB_TOKEN`` env var
    (local testing / SAM local) then Secrets Manager at
    ``RESURRECTOR_GITHUB_TOKEN_SECRET_ID`` (default
    ``/resurrector/github-token``). Secrets Manager values may be either a raw
    string or a JSON object containing a ``token`` / ``github_token`` key.

    ``secrets_client`` is injectable so tests never need AWS.
    """
    global _GITHUB_TOKEN_CACHE
    if use_cache and _GITHUB_TOKEN_CACHE:
        return _GITHUB_TOKEN_CACHE

    env_token = os.environ.get(GITHUB_TOKEN_ENV_VAR)
    if env_token:
        if use_cache:
            _GITHUB_TOKEN_CACHE = env_token
        return env_token

    resolved_id = secret_id or os.environ.get(
        GITHUB_TOKEN_SECRET_ID_ENV_VAR, DEFAULT_GITHUB_TOKEN_SECRET_ID
    )
    client = secrets_client if secrets_client is not None else _secrets_client()
    response = client.get_secret_value(SecretId=resolved_id)
    raw = response.get("SecretString")
    if not raw:
        raise ConfigurationError(f"secret {resolved_id!r} has no SecretString value")

    token = raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        token = parsed.get("token") or parsed.get("github_token") or ""
    if not token:
        raise ConfigurationError(f"secret {resolved_id!r} does not contain a token")

    if use_cache:
        _GITHUB_TOKEN_CACHE = token
    return token


# ---------------------------------------------------------------------------
# Eligibility (REQ-1.3, REQ-1.4)
# ---------------------------------------------------------------------------


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into aware UTC; ``None`` when unparseable."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_eligible(
    state: Optional[RepoState],
    *,
    now: Optional[datetime] = None,
    reevaluation_days: Optional[int] = None,
) -> bool:
    """Decide whether a candidate may be enqueued.

    Rules (see the module docstring for the reasoning behind each):

    - No record at all -> eligible. A brand-new candidate.
    - ``status`` in :data:`ACTIVE_STATUSES` (``in_progress``, ``pr_opened``) ->
      **not** eligible. This covers "already in progress", "PR already open",
      and "in active follow-up" (REQ-1.3), since escalation only happens while
      the repo is ``pr_opened``.
    - Any other status (``discovered``, ``skipped_complex``, ``fix_failed``,
      ``success``, ``rejected``, ``dormant``, ``ignored``) -> eligible only once
      ``last_action_at`` is strictly older than the re-evaluation window
      (default 30 days). REQ-1.4's ``closed`` maps onto ``rejected``/``ignored``
      here.
    - A record with a missing or unparseable ``last_action_at`` -> **not**
      eligible. We cannot prove the window elapsed, and treating it as eligible
      would re-enqueue the repo on every 6-hourly scan.

    ``now`` and ``reevaluation_days`` are injectable so callers and tests can
    pin the clock and the window.
    """
    if state is None:
        return True

    if state.status in ACTIVE_STATUSES:
        return False

    days = (
        reevaluation_days
        if reevaluation_days is not None
        else _env_int(REEVALUATION_DAYS_ENV_VAR, DEFAULT_REEVALUATION_DAYS)
    )
    reference = now if now is not None else _now()
    last_action = _parse_iso(state.last_action_at)
    if last_action is None:
        return False
    return (reference - last_action) > timedelta(days=days)


# ---------------------------------------------------------------------------
# SQS enqueue (REQ-1.5)
# ---------------------------------------------------------------------------


def build_message_body(candidate: RepoCandidate, *, discovered_at: datetime) -> str:
    """Serialize a candidate into the JSON body the Processor consumes.

    Keys are emitted in a stable order so the body — and therefore any hash of
    it — is deterministic for a given candidate and timestamp.
    """
    payload = {
        "repo_full_name": candidate.repo_full_name,
        "default_branch": candidate.default_branch,
        "open_issues": candidate.open_issues,
        "stars": candidate.stars,
        "html_url": candidate.html_url,
        "language": candidate.language,
        "last_commit_at": (
            candidate.last_commit_at.isoformat() if candidate.last_commit_at else None
        ),
        "discovered_at": discovered_at.isoformat(),
    }
    return json.dumps(payload, sort_keys=True)


def build_deduplication_id(
    repo_full_name: str,
    *,
    now: datetime,
    window_seconds: Optional[int] = None,
) -> str:
    """Build a deterministic content-based ``MessageDeduplicationId``.

    The candidate queue is provisioned with ``ContentBasedDeduplication: true``,
    but the message body embeds ``discovered_at``, so relying on the queue alone
    would make every scan look like fresh content. We therefore pass an explicit
    id: ``sha256(repo_full_name + ":" + time_bucket)`` where ``time_bucket`` is
    ``floor(epoch / window_seconds)`` with a default window of one 6-hour scan
    interval.

    The effect: a repo enqueued twice inside the same scan window collapses to a
    single message, while a legitimate re-evaluation in a later window gets
    through. The digest is 64 hex characters, well inside the SQS 128-character
    limit.
    """
    window = (
        window_seconds
        if window_seconds is not None
        else _env_int(DEDUP_WINDOW_ENV_VAR, DEFAULT_DEDUP_WINDOW_SECONDS)
    )
    window = max(1, window)
    bucket = int(now.timestamp()) // window
    digest = hashlib.sha256(f"{repo_full_name}:{bucket}".encode("utf-8"))
    return digest.hexdigest()


def enqueue_candidate(
    candidate: RepoCandidate,
    *,
    queue_url: Optional[str] = None,
    sqs_client: Any = None,
    now: Optional[datetime] = None,
    dedup_window_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Send one candidate to the candidate FIFO queue (REQ-1.5).

    Uses ``MessageGroupId = repo_full_name`` so all work for a repo is processed
    in order and different repos stay parallelizable, plus the deterministic
    dedup id from :func:`build_deduplication_id`.

    Returns the raw ``send_message`` response.
    """
    resolved_url = _queue_url(queue_url)
    reference = now if now is not None else _now()
    client = sqs_client if sqs_client is not None else _sqs_client()

    return client.send_message(
        QueueUrl=resolved_url,
        MessageBody=build_message_body(candidate, discovered_at=reference),
        MessageGroupId=candidate.repo_full_name,
        MessageDeduplicationId=build_deduplication_id(
            candidate.repo_full_name,
            now=reference,
            window_seconds=dedup_window_seconds,
        ),
    )


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------


def scan(
    *,
    github_client: Any = None,
    github_token: Optional[str] = None,
    sqs_client: Any = None,
    secrets_client: Any = None,
    queue_url: Optional[str] = None,
    table_name: Optional[str] = None,
    max_results: Optional[int] = None,
    reevaluation_days: Optional[int] = None,
    dedup_window_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
    search: Callable[..., list] = search_candidates,
) -> dict[str, Any]:
    """Run one full scan: search -> dedup -> enqueue.

    Every dependency is injectable so the whole flow can be exercised offline.
    Per-candidate failures are caught, logged, and counted so one bad repo never
    aborts the run (design.md section 10); the search call itself is allowed to
    raise, because without results there is nothing to do.

    Returns a JSON-serializable summary suitable for the CloudWatch log and for
    assertions in tests.
    """
    reference = now if now is not None else _now()
    resolved_url = _queue_url(queue_url)

    if github_client is None and github_token is None:
        github_token = load_github_token(secrets_client=secrets_client)

    candidates = search(
        client=github_client,
        token=github_token,
        max_results=max_results,
        now=reference,
    )

    summary: dict[str, Any] = {
        "scanned": len(candidates),
        "eligible": 0,
        "enqueued": 0,
        "skipped": 0,
        "errors": 0,
        "enqueued_repos": [],
        "skipped_repos": [],
        "error_repos": [],
    }

    for candidate in candidates:
        repo = candidate.repo_full_name
        try:
            state = dynamo_tools.read_state(repo, table_name=table_name)
            if not is_eligible(
                state, now=reference, reevaluation_days=reevaluation_days
            ):
                summary["skipped"] += 1
                summary["skipped_repos"].append(repo)
                LOGGER.info(
                    "skipping %s (status=%s)",
                    repo,
                    state.status if state else None,
                )
                continue

            summary["eligible"] += 1

            # Enqueue before writing state: see the module docstring for why we
            # tolerate a duplicate rather than a dropped candidate.
            enqueue_candidate(
                candidate,
                queue_url=resolved_url,
                sqs_client=sqs_client,
                now=reference,
                dedup_window_seconds=dedup_window_seconds,
            )
            dynamo_tools.transition(
                repo,
                "discovered",
                table_name=table_name,
                notes="enqueued by scanner",
            )

            summary["enqueued"] += 1
            summary["enqueued_repos"].append(repo)
            LOGGER.info("enqueued %s", repo)
        except Exception:  # noqa: BLE001 - one bad repo must not abort the run
            summary["errors"] += 1
            summary["error_repos"].append(repo)
            LOGGER.exception("failed to process candidate %s", repo)

    LOGGER.info(
        "scan complete: scanned=%s enqueued=%s skipped=%s errors=%s",
        summary["scanned"],
        summary["enqueued"],
        summary["skipped"],
        summary["errors"],
    )
    return summary


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:  # noqa: ARG001
    """CloudWatch cron entry point (REQ-1.1).

    The ``event`` payload is ignored apart from optional overrides that make
    manual invocations easy: ``max_results``, ``reevaluation_days``, and
    ``dedup_window_seconds``. Returns the :func:`scan` summary so the counts
    land in the CloudWatch log.
    """
    overrides: dict[str, Any] = {}
    if isinstance(event, dict):
        for key in ("max_results", "reevaluation_days", "dedup_window_seconds"):
            if event.get(key) is not None:
                overrides[key] = int(event[key])
    return scan(**overrides)
