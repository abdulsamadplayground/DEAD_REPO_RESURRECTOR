"""Orchestrator agent — owns the pipeline and is the only writer of state.

The Orchestrator is the "brain" of the system (design.md sections 1, 3, 5). It
consumes a dequeued candidate, drives Analyst → Engineer → Communicator, and is
the **only** component that writes DynamoDB state. It exposes two entry points:

- :func:`run` — the forward pipeline (REQ-2.4, REQ-4.4, REQ-6.1): analyse, gate,
  fix, open the PR, record ``pr_opened``, and signal the 7-day follow-up.
- :func:`handle_reply` — the reply/timer pipeline (REQ-5.*, REQ-6.2/6.3/6.4):
  act on already-verified maintainer activity, or on a fired follow-up timer.

Dual surface — why both a strands Agent *and* deterministic functions
---------------------------------------------------------------------
design.md section 3 is explicit that routing is the Orchestrator's *reasoning
loop*, not a hand-coded control-flow state machine: "the DynamoDB record is
durable state, not control flow". :func:`build_orchestrator` honours that — it
constructs a real ``strands.Agent`` whose ``tools`` are the three sub-agents
(agents-as-tools, design.md section 8) plus the two state helpers
``[analyst_agent, engineer_agent, communicator_agent, read_state,
write_state]``, and whose system prompt describes the pipeline, the gate, the
state machine, and the tone rules. That agent's model loop does the reasoning
and could re-order, retry, or skip steps.

But the whole system must *also* be deterministically runnable and testable when
Bedrock is unavailable — every prior task (Analyst, Engineer, Communicator)
built a model-free seam alongside its ``strands.Agent`` for exactly this reason
(design.md section 10). :func:`run` and :func:`handle_reply` are that seam for
the Orchestrator: they drive the pipeline explicitly over the sub-agents'
framework-free functions (:func:`~src.agents.analyst.analyze_repo`,
:func:`~src.agents.engineer.implement_fix`,
:func:`~src.agents.communicator.announce_pr`,
:func:`~src.agents.communicator.reply_to_maintainer`) with every model / GitHub
/ state seam injectable. This is the path the Processor Lambda (Task 9) actually
calls and the path the tests exercise. It is not a *second* routing brain that
contradicts the design — it is the durable, offline-safe realisation of the same
happy path the section-5 sequence diagram already fixes, mirroring how
``analyze_repo`` (deterministic) coexists with ``build_analyst`` (strands) in
Task 5. Both surfaces share the same sub-agent tools and the same state helpers.

Only-writer invariant (design.md section 3)
-------------------------------------------
The Orchestrator is the only place :func:`src.tools.dynamo_tools.transition` /
``write_state`` / :func:`~src.tools.dynamo_tools.increment_follow_up` are called.
Every sub-agent *returns* a recommended status; the Orchestrator performs every
state write. A test asserts by AST that analyst/engineer/communicator do not
import ``dynamo_tools`` while this module does, and a spy test asserts every
state write in a full flow goes through the injected state seam.

AWS messaging is signalled, not performed (REQ-6.1, REQ-6.4)
------------------------------------------------------------
The Orchestrator's *only* AWS side effect is DynamoDB. Follow-up scheduling
(SQS ``DelaySeconds``) and operator alerts (SNS) are **signalled** in the return
value — :attr:`OrchestrationResult.follow_up` and :attr:`OrchestrationResult.sns`
— for the Processor (Task 9) to perform. Rationale: it keeps the Orchestrator
free of SQS/SNS calls (matching design.md section 3, "Orchestrator owns state"),
keeps it unit-testable without those services, and puts the SQS enqueue where
the Processor already owns queue interaction. The Processor holds the
``sns:Publish`` and ``sqs:SendMessage`` IAM grants; the Orchestrator does not
need them. (The follow-up comment itself *is* posted here, via the Communicator,
because posting a comment is a GitHub action the pipeline owns — only the
*timer* and the *alert* are deferred.)

REQ-5.1 boundary
----------------
Webhook signature verification and maintainer identification are the Webhook
Lambda's job (Task 10). :func:`handle_reply` receives an **already-verified**
event carrying an ``is_maintainer`` flag and acts on it; it never verifies
signatures itself.

Verification status
-------------------
Everything below is exercised offline with injected sub-agents and an injected
state layer. The live pieces that no offline test can cover — the Bedrock
round-trip inside :func:`build_orchestrator`'s agent, and real
GitHub/DynamoDB/SNS/SQS calls — remain unverified without credentials (Task 13).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.agents.analyst import analyst_agent, analyze_repo
from src.agents.communicator import (
    announce_pr,
    communicator_agent,
    post_follow_up,
    reply_to_maintainer,
)
from src.agents.engineer import engineer_agent, implement_fix
from src.tools import dynamo_tools, pr_templates

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded strands import (mirrors the three sub-agents)
# ---------------------------------------------------------------------------

# strands is a core dependency: the Orchestrator *is* a strands.Agent (design.md
# sections 3 and 8). The guard mirrors the sub-agents — it keeps a Lambda cold
# start or a stripped CI image from hard-failing at import time, and it keeps the
# deterministic run()/handle_reply() pipeline working when the model layer is
# unavailable. It is not an invitation to run without strands.
try:  # pragma: no cover - exercised by whichever branch the environment has
    from strands import Agent as _StrandsAgent
    from strands import tool as _strands_tool

    STRANDS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _StrandsAgent = None  # type: ignore[assignment]
    _strands_tool = None  # type: ignore[assignment]
    STRANDS_AVAILABLE = False


class StrandsUnavailableError(RuntimeError):
    """Raised when a strands-only code path is used without strands installed."""


def tool(func: Callable[..., Any]) -> Any:
    """Apply ``strands.tool`` when available, otherwise return ``func`` unchanged.

    Same identity fallback as the sub-agents: without ``strands`` the state
    adapters below stay ordinary callables so the module imports and unit-tests
    cleanly; with ``strands`` the real decorator produces proper tool specs.
    """
    if _strands_tool is not None:
        return _strands_tool(func)
    return func


# ---------------------------------------------------------------------------
# Constants (REQ-6.1, REQ-6.2, REQ-6.3)
# ---------------------------------------------------------------------------

#: SQS DelaySeconds for the 7-day follow-up timer (REQ-6.1).
FOLLOW_UP_7D_SECONDS = 604800
#: SQS DelaySeconds for the 14-day secondary timer (REQ-6.2).
FOLLOW_UP_14D_SECONDS = 1209600
#: The message-type attribute the Processor routes follow-up timers by.
FOLLOW_UP_MESSAGE_TYPE = "follow_up"

#: Optional pause (seconds) inserted between the Analyst and the Engineer so the
#: two model-invoking stages don't share one provider rate-limit minute (the
#: deployed system runs on a Gemini free tier of 5 requests/minute). Default 0 →
#: no pause, behaviour identical to before this knob existed.
MODEL_PACE_ENV_VAR = "RESURRECTOR_MODEL_PACE_SECONDS"


def _model_pace_seconds() -> int:
    """Read the model-pacing pause (seconds) from the environment.

    Mirrors the ``_env_*`` convention used across the tool modules: unset, empty,
    or non-integer values fall back to ``0`` (no pause).
    """
    raw = os.environ.get(MODEL_PACE_ENV_VAR)
    if raw is None or raw.strip() == "":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# State-helper tool adapters (design.md section 3: read_state / write_state)
# ---------------------------------------------------------------------------


@tool
def read_state(repo_full_name: str) -> str:
    """Read one repository's current DynamoDB record. Returns JSON (or ``null``).

    This is the read half of the Orchestrator's state tools (design.md section
    3). It is the model-facing view; the deterministic pipeline calls
    :func:`src.tools.dynamo_tools.read_state` directly.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.

    Returns:
        A JSON object of the stored attributes, or ``null`` when no record
        exists yet.
    """
    record = dynamo_tools.read_state(repo_full_name)
    return json.dumps(record.to_item() if record is not None else None)


@tool
def write_state(repo_full_name: str, status: str, notes: str = "") -> str:
    """Persist a status transition for a repository. Returns the new record JSON.

    This is the write half of the Orchestrator's state tools (design.md section
    3) and the *only* sanctioned state write in the whole system. It delegates
    to :func:`src.tools.dynamo_tools.transition`, which validates the status,
    stamps ``last_action_at`` (REQ-7.1), and upserts atomically.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        status: The new status; must be one of ``dynamo_tools.VALID_STATUSES``.
        notes: Optional free-form reasoning summary to store.

    Returns:
        A JSON object of the post-update record.
    """
    updates: dict[str, Any] = {}
    if notes:
        updates["notes"] = notes
    record = dynamo_tools.transition(repo_full_name, status, **updates)
    return json.dumps(record.to_item())


#: The Orchestrator's tool set, in the design.md section-3 order. Exactly five:
#: the three agents-as-tools plus the two state helpers.
ORCHESTRATOR_TOOLS = (
    analyst_agent,
    engineer_agent,
    communicator_agent,
    read_state,
    write_state,
)


# ---------------------------------------------------------------------------
# System prompt (design.md sections 3, 4, 5 + NFR safety / tone)
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = f"""\
You are the Orchestrator of an autonomous open-source contribution pipeline.

You receive one repository that {pr_templates.REDUCED_MAINTENANCE_PHRASE} and
you coordinate three specialists to turn its top open issue into a respectful,
validated pull request. You are the only component that records state.

Your tools:
  - analyst_agent       read the repo, pick the top issue, score its complexity
  - engineer_agent      create the fix branch, author + validate the fix, push
  - communicator_agent  open the PR and announce it on the issue
  - read_state          read a repository's current DynamoDB record
  - write_state         record a status transition (the ONLY way state changes)

The pipeline, in order:
  1. Record the repo as in_progress.
  2. Ask the Analyst to score the top issue. It returns a gate decision.
     - If the gate refuses (complexity is complex, or confidence is below the
       threshold), record skipped_complex and stop. Do not overreach.
     - Otherwise continue.
  3. Ask the Engineer to implement the fix.
     - If the Engineer could not produce a valid, tested fix, record fix_failed
       and stop. A plausible-looking wrong patch costs a maintainer their time.
     - Otherwise continue.
  4. Ask the Communicator to open the PR and comment on the issue.
     - On success, record pr_opened with the PR number, PR URL, issue number,
       and opened_at timestamp.

State machine (record durable state, never treat it as control flow):
  discovered -> in_progress -> skipped_complex   (Analyst refused)
                            -> fix_failed         (Engineer failed / no PR)
                            -> pr_opened -> success   (maintainer merged)
                                         -> rejected  (maintainer closed unmerged)
                                         -> dormant   (no reply after 7d + 14d)

Tone and safety (non-negotiable):
  - Never call a project or its maintainers "dead", "abandoned", or
    "unmaintained". Say it "{pr_templates.REDUCED_MAINTENANCE_PHRASE}".
  - Never pressure a maintainer. Every offer of help is opt-in with an explicit
    no-obligation out.
  - Prefer doing nothing to doing something low-quality. Declining is cheap.
"""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationResult:
    """The outcome of one :func:`run` or :func:`handle_reply` invocation.

    Fully JSON-serializable in every branch. ``follow_up`` and ``sns`` are the
    intents the Processor performs (the Orchestrator only signals them — see the
    module docstring). ``stage_reached`` records how far the pipeline got:
    ``analyst | engineer | communicator | done`` for :func:`run`, and
    ``reply | follow_up`` for :func:`handle_reply`.
    """

    repo_full_name: str
    status: Optional[str]
    stage_reached: str
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    branch: Optional[str] = None
    maintainer_responded: Optional[bool] = None
    follow_up_count: Optional[int] = None
    follow_up: Optional[dict[str, Any]] = None
    sns: Optional[dict[str, Any]] = None
    notes: str = ""
    classification: Optional[dict[str, Any]] = None
    analyst: Optional[dict[str, Any]] = None
    engineer: Optional[dict[str, Any]] = None
    communicator: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "status": self.status,
            "stage_reached": self.stage_reached,
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "maintainer_responded": self.maintainer_responded,
            "follow_up_count": self.follow_up_count,
            "follow_up": self.follow_up,
            "sns": self.sns,
            "notes": self.notes,
            "classification": self.classification,
            "analyst": self.analyst,
            "engineer": self.engineer,
            "communicator": self.communicator,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string."""
        return json.dumps(self.to_dict(), **kwargs)


def _follow_up_intent(stage: int) -> dict[str, Any]:
    """Build the follow-up timer intent for the Processor (REQ-6.1 / REQ-6.2)."""
    delay = FOLLOW_UP_7D_SECONDS if stage == 7 else FOLLOW_UP_14D_SECONDS
    return {
        "delay_seconds": delay,
        "message_type": FOLLOW_UP_MESSAGE_TYPE,
        "stage": stage,
    }


# ---------------------------------------------------------------------------
# PR narrative (Orchestrator-generated text — must obey the tone rules)
# ---------------------------------------------------------------------------


def _pr_narrative(
    *, issue_number: Optional[int], approach: str, engineer_result: Any
) -> tuple[str, str, str]:
    """Compose the (what_changed, why, how_to_test) strings for the PR.

    The exact PR title/body wording is the Communicator's templates; this only
    supplies the *substance* the Engineer and Analyst produced. Every default
    here is safe (no banned word) so an empty sub-result never yields awkward or
    prohibited text.
    """
    what_changed = (
        (getattr(engineer_result, "notes", "") or "").strip()
        or (getattr(engineer_result, "reason", "") or "").strip()
        or "A small, targeted fix for the reported issue."
    )

    why = (approach or "").strip()
    if not why:
        why = (
            f"Addresses the behaviour described in issue #{issue_number}."
            if issue_number is not None
            else "Addresses the behaviour described in the linked issue."
        )

    test_result = getattr(engineer_result, "test_result", None) or {}
    outcome = test_result.get("outcome") if isinstance(test_result, dict) else None
    if outcome == "passed":
        how_to_test = "Run the project's test suite; this change keeps it green."
    elif outcome in {"no_tests_found", "not_executed", None}:
        how_to_test = (
            "Review the change and run the project's test suite to confirm the "
            "reported behaviour is resolved."
        )
    else:
        how_to_test = (
            "Run the project's test suite and exercise the scenario from the "
            "issue to confirm the fix."
        )
    return what_changed, why, how_to_test


# ---------------------------------------------------------------------------
# Forward pipeline — run(message) (REQ-2.4, REQ-4.4, REQ-6.1)
# ---------------------------------------------------------------------------


def run(
    message: dict[str, Any],
    *,
    analyze: Optional[Callable[..., Any]] = None,
    implement: Optional[Callable[..., Any]] = None,
    announce: Optional[Callable[..., Any]] = None,
    transition: Optional[Callable[..., Any]] = None,
    run_model: Optional[Callable[[str], str]] = None,
    client: Any = None,
    token: Optional[str] = None,
    threshold: Optional[float] = None,
    table_name: Optional[str] = None,
    pace_sleep: Callable[[float], None] = time.sleep,
    pace_seconds: Optional[int] = None,
) -> OrchestrationResult:
    """Drive one candidate through Analyst → Engineer → Communicator.

    ``message`` is the body the Scanner enqueues (``repo_full_name`` plus
    optional ``default_branch``, ``issue_body``, etc.). The five steps follow
    design.md section 5:

    1. Record ``in_progress`` (discovered → in_progress).
    2. Analyst scores the top issue. Gate refuses → ``skipped_complex``, stop
       (the Engineer is never called).
    3. Engineer implements the fix (REQ-2.4). Failure → ``fix_failed``, stop
       (the Communicator is never called).
    4. Communicator opens the PR + issue comment. PR-open failure →
       ``fix_failed`` with a note (the fix is on a branch but never reached a
       PR; among the valid ``in_progress`` exits this is the honest terminal
       state, and it lets a redelivered message retry).
    5. Success → ``pr_opened`` with ``pr_number`` / ``pr_url`` / ``issue_number``
       / ``opened_at`` (REQ-4.4), and a 7-day follow-up intent is returned
       (REQ-6.1) for the Processor to enqueue — no SQS call is made here.

    A raised sub-agent exception is caught and recorded as a terminal state
    (``skipped_complex`` at the Analyst, ``fix_failed`` later) rather than
    crashing (design.md section 10).

    Every seam is injectable so this runs fully offline: ``analyze`` /
    ``implement`` / ``announce`` replace the sub-agent calls, ``transition``
    replaces the state write, and ``run_model`` / ``client`` / ``token`` /
    ``threshold`` are threaded into the default sub-agent calls otherwise.

    ``pace_seconds`` (``None`` → read ``RESURRECTOR_MODEL_PACE_SECONDS``, default
    0) inserts an optional pause between the Analyst and the Engineer so the two
    model-invoking stages don't share one provider rate-limit minute; ``pace_sleep``
    (default :func:`time.sleep`) is injectable so tests never actually sleep. The
    pause fires only on the proceed→Engineer path — never when the gate refuses
    or the Analyst errors. A resolved pace of 0 means behaviour is identical to
    before this knob existed.
    """
    repo = message.get("repo_full_name")
    if not repo:
        raise ValueError("message is missing 'repo_full_name'")

    _transition = transition or dynamo_tools.transition

    def _do_transition(status: str, **updates: Any):
        return _transition(repo, status, table_name=table_name, **updates)

    _analyze = analyze or (
        lambda repo_full_name: analyze_repo(
            repo_full_name,
            client=client,
            token=token,
            run_model=run_model,
            threshold=threshold,
        )
    )
    _implement = implement or (
        lambda repo_full_name, issue_number, **kw: implement_fix(
            repo_full_name,
            issue_number,
            client=client,
            token=token,
            run_model=run_model,
            **kw,
        )
    )
    _announce = announce or (
        lambda repo_full_name, **kw: announce_pr(
            repo_full_name, client=client, token=token, **kw
        )
    )

    # -- 1. discovered -> in_progress ------------------------------------
    _do_transition("in_progress")

    # -- 2. Analyst ------------------------------------------------------
    try:
        report = _analyze(repo)
    except Exception as exc:  # noqa: BLE001 - design.md §10: record, never crash
        LOGGER.warning("Analyst raised for %s: %s", repo, exc)
        note = f"analyst error ({exc.__class__.__name__}); skipped"
        _do_transition("skipped_complex", notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="skipped_complex",
            stage_reached="analyst",
            notes=note,
        )

    analyst_dict = report.to_dict()
    if not report.gate.proceed:
        note = report.gate.reason or "gate refused"
        complexity = report.gate.complexity
        updates: dict[str, Any] = {"notes": note}
        if complexity:
            updates["complexity"] = complexity
        _do_transition("skipped_complex", **updates)
        return OrchestrationResult(
            repo_full_name=repo,
            status="skipped_complex",
            stage_reached="analyst",
            issue_number=report.issue_number,
            notes=note,
            analyst=analyst_dict,
        )

    # -- 3. Engineer (REQ-2.4: trivial|moderate proceed) -----------------
    # The gate said proceed and the Engineer is about to run. The Analyst and
    # the Engineer are the two model-invoking stages; on a per-minute rate limit
    # (Gemini free tier = 5 req/min) running them back-to-back can breach it. If
    # a pause is configured, wait here — only on the proceed→Engineer path, never
    # when the gate refused or the Analyst errored above.
    pace = pace_seconds if pace_seconds is not None else _model_pace_seconds()
    if pace > 0:
        LOGGER.info("pacing %ss before the Engineer to respect model rate limits", pace)
        pace_sleep(pace)

    approach = report.result.approach if report.result else ""
    files_affected = report.result.files_affected if report.result else []
    complexity = report.result.complexity if report.result else None
    try:
        eng = _implement(
            repo,
            report.issue_number,
            issue_title=report.issue_title or "",
            issue_body=message.get("issue_body"),
            approach=approach,
            files_affected=files_affected,
        )
    except Exception as exc:  # noqa: BLE001 - design.md §10
        LOGGER.warning("Engineer raised for %s: %s", repo, exc)
        note = f"engineer error ({exc.__class__.__name__}); fix_failed"
        updates = {"notes": note}
        if complexity:
            updates["complexity"] = complexity
        _do_transition("fix_failed", **updates)
        return OrchestrationResult(
            repo_full_name=repo,
            status="fix_failed",
            stage_reached="engineer",
            issue_number=report.issue_number,
            notes=note,
            analyst=analyst_dict,
        )

    engineer_dict = eng.to_dict()
    if not eng.success:
        note = f"engineer could not complete the fix: {eng.reason}"
        updates = {"notes": note}
        if complexity:
            updates["complexity"] = complexity
        _do_transition("fix_failed", **updates)
        return OrchestrationResult(
            repo_full_name=repo,
            status="fix_failed",
            stage_reached="engineer",
            issue_number=report.issue_number,
            branch=eng.branch,
            notes=note,
            analyst=analyst_dict,
            engineer=engineer_dict,
        )

    # -- 4. Communicator -------------------------------------------------
    what_changed, why, how_to_test = _pr_narrative(
        issue_number=report.issue_number,
        approach=approach,
        engineer_result=eng,
    )
    try:
        pr_result, comment_result = _announce(
            repo,
            head_branch=eng.branch,
            issue_number=report.issue_number,
            issue_title=report.issue_title or "",
            what_changed=what_changed,
            why=why,
            how_to_test=how_to_test,
        )
    except Exception as exc:  # noqa: BLE001 - design.md §10
        LOGGER.warning("Communicator raised for %s: %s", repo, exc)
        note = f"PR open error ({exc.__class__.__name__}); fix pushed but no PR"
        _do_transition("fix_failed", notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="fix_failed",
            stage_reached="communicator",
            issue_number=report.issue_number,
            branch=eng.branch,
            notes=note,
            analyst=analyst_dict,
            engineer=engineer_dict,
        )

    communicator_dict = {
        "pr": pr_result.to_dict(),
        "issue_comment": comment_result.to_dict() if comment_result else None,
    }
    if not pr_result.success:
        note = f"fix pushed to {eng.branch} but PR open failed: {pr_result.reason}"
        _do_transition("fix_failed", notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="fix_failed",
            stage_reached="communicator",
            issue_number=report.issue_number,
            branch=eng.branch,
            notes=note,
            analyst=analyst_dict,
            engineer=engineer_dict,
            communicator=communicator_dict,
        )

    # -- 5. pr_opened (REQ-4.4) + 7-day follow-up intent (REQ-6.1) -------
    note = f"opened PR #{pr_result.pr_number} for issue #{report.issue_number}"
    _do_transition(
        "pr_opened",
        pr_number=pr_result.pr_number,
        pr_url=pr_result.pr_url,
        issue_number=report.issue_number,
        opened_at=pr_result.opened_at,
        maintainer_responded=False,
        follow_up_count=0,
        notes=note,
    )
    return OrchestrationResult(
        repo_full_name=repo,
        status="pr_opened",
        stage_reached="done",
        issue_number=report.issue_number,
        pr_number=pr_result.pr_number,
        pr_url=pr_result.pr_url,
        branch=eng.branch,
        maintainer_responded=False,
        follow_up_count=0,
        follow_up=_follow_up_intent(7),
        notes=note,
        analyst=analyst_dict,
        engineer=engineer_dict,
        communicator=communicator_dict,
    )


# ---------------------------------------------------------------------------
# Reply / timer pipeline — handle_reply(event) (REQ-5.*, REQ-6.2/6.3/6.4)
# ---------------------------------------------------------------------------


def handle_reply(
    event: dict[str, Any],
    *,
    read_state_fn: Optional[Callable[..., Any]] = None,
    transition: Optional[Callable[..., Any]] = None,
    increment: Optional[Callable[..., Any]] = None,
    reply: Optional[Callable[..., Any]] = None,
    post_follow_up_fn: Optional[Callable[..., Any]] = None,
    run_model: Optional[Callable[[str], str]] = None,
    client: Any = None,
    token: Optional[str] = None,
    table_name: Optional[str] = None,
) -> OrchestrationResult:
    """Act on an already-verified maintainer event or a fired follow-up timer.

    Two event kinds are dispatched on ``message_type``:

    **Follow-up timer** (``message_type == "follow_up"``, ``stage`` 7 or 14):
    delegated to :func:`_handle_follow_up`.

    **Maintainer activity** (anything else, from the Webhook Lambda):
    ``repo_full_name``, ``pr_number``, ``issue_number``, ``merged`` (bool),
    ``state`` / ``action``, comment ``text``, and ``is_maintainer``. Only acts
    when the repo is in ``pr_opened`` (REQ-5.1 precondition); otherwise no-op.

    See :func:`_handle_maintainer_activity` for the per-classification rules.
    All state / GitHub / model seams are injectable for offline testing.
    """
    repo = event.get("repo_full_name")
    if not repo:
        raise ValueError("event is missing 'repo_full_name'")

    _read = read_state_fn or dynamo_tools.read_state
    _transition = transition or dynamo_tools.transition
    _increment = increment or dynamo_tools.increment_follow_up
    _reply = reply or reply_to_maintainer
    _post_follow_up = post_follow_up_fn or post_follow_up

    record = _read(repo, table_name=table_name)

    if event.get("message_type") == FOLLOW_UP_MESSAGE_TYPE:
        return _handle_follow_up(
            repo,
            event,
            record=record,
            increment=lambda status=None, **kw: _increment(
                repo, new_status=status, table_name=table_name, **kw
            ),
            post_follow_up_fn=_post_follow_up,
            client=client,
            token=token,
        )

    return _handle_maintainer_activity(
        repo,
        event,
        record=record,
        transition=lambda status, **kw: _transition(
            repo, status, table_name=table_name, **kw
        ),
        reply=_reply,
        run_model=run_model,
        client=client,
        token=token,
    )


def _not_open_noop(repo: str, record: Any, stage_reached: str) -> OrchestrationResult:
    """Build the no-op result for an event on a repo that is not ``pr_opened``."""
    current = record.status if record is not None else "absent"
    note = f"no-op: {repo} is {current!r}, not 'pr_opened'"
    return OrchestrationResult(
        repo_full_name=repo,
        status=current if record is not None else None,
        stage_reached=stage_reached,
        pr_number=record.pr_number if record is not None else None,
        maintainer_responded=(
            record.maintainer_responded if record is not None else None
        ),
        notes=note,
    )


def _handle_maintainer_activity(
    repo: str,
    event: dict[str, Any],
    *,
    record: Any,
    transition: Callable[..., Any],
    reply: Callable[..., Any],
    run_model: Optional[Callable[[str], str]],
    client: Any,
    token: Optional[str],
) -> OrchestrationResult:
    """REQ-5.* — classify and act on already-verified maintainer activity.

    Rules:
      - not ``pr_opened`` → no-op (REQ-5.1 precondition).
      - merged → ``success``, stop follow-up (REQ-5.2).
      - closed unmerged → ``rejected``, stop follow-up (REQ-5.4).
      - question → Communicator posts a reply (REQ-5.3); set
        ``maintainer_responded=True`` and keep ``pr_opened``. A maintainer who
        asked a question *has* engaged, so ``maintainer_responded=True`` means
        the 7-day nudge will not fire (REQ-6.2 gate) — the follow-up clock stops.
      - any other maintainer comment → set ``maintainer_responded=True``, no
        status change. A non-maintainer actor is recorded as a no-op note.
    """
    if record is None or record.status != "pr_opened":
        return _not_open_noop(repo, record, "reply")

    pr_number = event.get("pr_number") or record.pr_number
    merged = bool(event.get("merged"))
    pr_state = event.get("state")
    if not pr_state and event.get("action") == "closed":
        pr_state = "closed"
    text = event.get("text")
    is_maintainer = bool(event.get("is_maintainer", True))

    classification = pr_templates.classify_maintainer_reply(
        text=text, merged=merged if merged else None, state=pr_state
    )
    cls = classification.classification

    # A merge/close is ground truth regardless of who is flagged; a mere comment
    # only counts as "responded" when it is the maintainer.
    if cls == "merged":
        note = f"maintainer merged PR #{pr_number}; marking success"
        transition("success", maintainer_responded=True, notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="success",
            stage_reached="reply",
            issue_number=record.issue_number,
            pr_number=pr_number,
            pr_url=record.pr_url,
            maintainer_responded=True,
            notes=note,
            classification=classification.to_dict(),
        )

    if cls == "closed":
        note = f"maintainer closed PR #{pr_number} unmerged; marking rejected"
        transition("rejected", maintainer_responded=True, notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="rejected",
            stage_reached="reply",
            issue_number=record.issue_number,
            pr_number=pr_number,
            pr_url=record.pr_url,
            maintainer_responded=True,
            notes=note,
            classification=classification.to_dict(),
        )

    if cls == "question":
        _, comment = reply(
            repo,
            pr_number,
            text=text,
            merged=merged if merged else None,
            state=pr_state,
            run_model=run_model,
            client=client,
            token=token,
        )
        note = f"answered maintainer question on PR #{pr_number}"
        # maintainer_responded=True stops the 7-day nudge (REQ-6.2 gate).
        transition("pr_opened", maintainer_responded=True, notes=note)
        return OrchestrationResult(
            repo_full_name=repo,
            status="pr_opened",
            stage_reached="reply",
            issue_number=record.issue_number,
            pr_number=pr_number,
            pr_url=record.pr_url,
            maintainer_responded=True,
            notes=note,
            classification=classification.to_dict(),
            communicator={"reply": comment.to_dict() if comment else None},
        )

    # comment / other
    if is_maintainer:
        note = f"maintainer commented on PR #{pr_number}; noting engagement"
        transition("pr_opened", maintainer_responded=True, notes=note)
        responded = True
    else:
        note = f"non-maintainer activity on PR #{pr_number}; no state change"
        responded = record.maintainer_responded

    return OrchestrationResult(
        repo_full_name=repo,
        status="pr_opened",
        stage_reached="reply",
        issue_number=record.issue_number,
        pr_number=pr_number,
        pr_url=record.pr_url,
        maintainer_responded=responded,
        notes=note,
        classification=classification.to_dict(),
    )


def _handle_follow_up(
    repo: str,
    event: dict[str, Any],
    *,
    record: Any,
    increment: Callable[..., Any],
    post_follow_up_fn: Callable[..., Any],
    client: Any,
    token: Optional[str],
) -> OrchestrationResult:
    """REQ-6.2 / REQ-6.3 / REQ-6.4 — act on a fired follow-up timer.

    - The timer only fires an action while the repo is still ``pr_opened`` with
      ``maintainer_responded=False``; otherwise it is a no-op (the maintainer
      engaged, or the state already moved on).
    - **7-day** (REQ-6.2): post the single follow-up comment via the
      Communicator, increment ``follow_up_count`` (REQ-6.4), signal an SNS
      escalation (REQ-6.4), and signal the 14-day secondary timer.
    - **14-day** (REQ-6.3): mark ``dormant``, increment ``follow_up_count``,
      signal SNS, and stop follow-up.

    ``follow_up_count`` is incremented atomically via
    :func:`src.tools.dynamo_tools.increment_follow_up` (an ``ADD`` update) rather
    than a read-modify-write, because a timer event and a webhook event can touch
    the same repo concurrently and both may adjust the counter/status.
    """
    stage = int(event.get("stage", 7))

    if record is None or record.status != "pr_opened" or record.maintainer_responded:
        current = record.status if record is not None else "absent"
        responded = record.maintainer_responded if record is not None else None
        note = (
            f"follow-up no-op: {repo} is {current!r}, "
            f"maintainer_responded={responded}"
        )
        return OrchestrationResult(
            repo_full_name=repo,
            status=current if record is not None else None,
            stage_reached="follow_up",
            pr_number=record.pr_number if record is not None else None,
            maintainer_responded=responded,
            notes=note,
        )

    pr_number = record.pr_number

    if stage >= 14:
        note = f"no maintainer response after follow-ups; marking {repo} dormant"
        updated = increment(status="dormant", notes=note)
        sns = {
            "subject": f"[Resurrector] {repo} marked dormant",
            "message": (
                f"No maintainer response on {repo} PR #{pr_number} after the "
                f"7-day and 14-day follow-ups. Marked dormant; stopping follow-up."
            ),
        }
        return OrchestrationResult(
            repo_full_name=repo,
            status="dormant",
            stage_reached="follow_up",
            issue_number=record.issue_number,
            pr_number=pr_number,
            pr_url=record.pr_url,
            maintainer_responded=False,
            follow_up_count=updated.follow_up_count,
            sns=sns,
            notes=note,
        )

    # 7-day escalation (REQ-6.2)
    comment = post_follow_up_fn(
        repo, pr_number, stage=7, client=client, token=token
    )
    note = f"posted the 7-day follow-up on {repo} PR #{pr_number}"
    updated = increment(notes=note)
    sns = {
        "subject": f"[Resurrector] 7-day follow-up on {repo}",
        "message": (
            f"Posted a 7-day follow-up on {repo} PR #{pr_number}; no maintainer "
            f"response yet. Scheduled a 14-day check. follow_up_count="
            f"{updated.follow_up_count}."
        ),
    }
    return OrchestrationResult(
        repo_full_name=repo,
        status="pr_opened",
        stage_reached="follow_up",
        issue_number=record.issue_number,
        pr_number=pr_number,
        pr_url=record.pr_url,
        maintainer_responded=False,
        follow_up_count=updated.follow_up_count,
        follow_up=_follow_up_intent(14),
        sns=sns,
        notes=note,
        communicator={"follow_up": comment.to_dict() if comment else None},
    )


# ---------------------------------------------------------------------------
# Agent construction (design.md section 8)
# ---------------------------------------------------------------------------


def build_orchestrator(
    *,
    model: Any = None,
    tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """Construct the Orchestrator ``strands.Agent`` (design.md sections 3, 8).

    Its ``tools`` are the three sub-agents-as-tools plus the two state helpers —
    exactly ``[analyst_agent, engineer_agent, communicator_agent, read_state,
    write_state]`` — so the model loop can route among them. ``model`` is passed
    through so the caller can pin a specific Bedrock model; ``None`` lets strands
    apply its configured default.

    Raises:
        StrandsUnavailableError: when ``strands`` is not installed. The
            deterministic :func:`run` / :func:`handle_reply` pipeline still works
            without it — that is the whole point of the dual surface.
    """
    if not STRANDS_AVAILABLE or _StrandsAgent is None:
        raise StrandsUnavailableError(
            "strands-agents is not installed; the model-backed Orchestrator is "
            "unavailable. run(message) and handle_reply(event) still work."
        )
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt or ORCHESTRATOR_SYSTEM_PROMPT,
        "tools": list(tools) if tools is not None else list(ORCHESTRATOR_TOOLS),
    }
    if model is not None:
        kwargs["model"] = model
    return _StrandsAgent(**kwargs)
