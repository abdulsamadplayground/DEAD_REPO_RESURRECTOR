"""Communicator sub-agent — opens the PR, comments, triages maintainer replies.

The Communicator is the third stage of the Orchestrator's pipeline (design.md
sections 3 and 5) and the **only** agent that opens pull requests or posts
comments. It runs after the Engineer has pushed a valid fix branch:

- REQ-4.1 open a PR titled ``[Resurrector] Fix: {issue title} (closes #{N})``
- REQ-4.2 the body has *what changed*, *why*, *how to test*, and a respectful
  co-maintenance offer, and references the issue with ``closes #{N}``
- REQ-4.3 comment on the original issue, linking the PR
- REQ-5.3 when a maintainer asks a question, post a helpful, precise reply

It does **not** write code, create branches, or write DynamoDB state. Those are
the Engineer's and the Orchestrator's jobs (design.md section 3). A test asserts
this module's source contains no branch/file-write call and never imports the
state layer, so the boundary is checked rather than merely documented.

Layering
--------
All business logic lives in framework-free modules, exactly as the Analyst and
Engineer do it:

- :mod:`src.tools.pr_templates` — every string a maintainer reads (PR title/body,
  issue comment, follow-up, reply classification). Pure stdlib, so the
  github-conventions.md wording is exhaustively testable and the banned words
  live in one auditable place.
- :mod:`src.tools.github_comms` — the PyGithub write calls (``create_pull``,
  ``create_comment``) that carry those strings to GitHub, reusing
  ``resolve_client`` and ``with_backoff``.

This module holds only the ``strands`` wiring: the four ``@tool`` adapters from
design.md section 3, the system prompt (embodying the tone rules), the
orchestrating :func:`announce_pr` / :func:`reply_to_maintainer` seams,
:func:`build_communicator`, and the agent-as-tool entry point
:func:`communicator_agent` (design.md section 8).

REQ-4.4 boundary
----------------
REQ-4.4 records ``pr_opened`` with ``pr_number`` / ``pr_url`` / ``issue_number`` /
``opened_at``, but the Orchestrator is the only component that writes state
(design.md section 3). The Communicator **returns** those fields on the
:class:`~src.tools.github_comms.PROpenResult` for the Orchestrator (Task 8) to
persist. Nothing here imports ``dynamo_tools``, and a test asserts that.

Verification status
-------------------
Everything below is exercised offline with fake PyGithub objects and an injected
``run_model``. Two things cannot be verified without live credentials and are
honestly unverified here: the Bedrock round-trip behind ``agent(prompt)``, and a
real PR/comment landing on github.com. Task 13 covers both against a private test
repo.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from src.tools import github_comms, pr_templates
from src.tools.github_comms import CommentResult, PROpenResult
from src.tools.pr_templates import ReplyClassification

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded strands import
# ---------------------------------------------------------------------------

# strands is a core dependency: the Communicator *is* a strands.Agent (design.md
# sections 3 and 8). The guard mirrors src.agents.analyst / src.agents.engineer —
# it keeps a Lambda cold start or a stripped CI image from hard-failing at import
# time, and it keeps the deterministic paths (template formatting, reply
# classification) working when the model layer is unavailable. It is not an
# invitation to run without strands.
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

    Same identity fallback as the Analyst and Engineer: without ``strands`` the
    adapters stay ordinary callables, so the module imports and unit-tests
    cleanly; with ``strands`` the real decorator produces proper tool specs.
    """
    if _strands_tool is not None:
        return _strands_tool(func)
    return func


# ---------------------------------------------------------------------------
# System prompt (design.md section 3 + github-conventions.md tone rules)
# ---------------------------------------------------------------------------

#: The Communicator's instructions. The wording bakes in every github-conventions.md
#: tone rule: never call a repo dead/abandoned/unmaintained (use the mandated
#: "appears to have reduced maintenance activity"), never pressure the maintainer,
#: always leave a no-obligation out, and answer a question before linking a diff.
COMMUNICATOR_SYSTEM_PROMPT = f"""\
You are the Communicator in an autonomous open-source contribution pipeline.

An Engineer has already pushed a validated fix to a branch on a repository that
{pr_templates.REDUCED_MAINTENANCE_PHRASE}. Your job is to open a clear,
respectful pull request, tell the issue's followers about it, and — later —
answer the maintainer helpfully if they respond.

You may use these tools:
  - open_pr                    open the PR for the fix branch
  - post_issue_comment         comment on the original issue, linking the PR
  - post_pr_comment            reply on the PR conversation
  - classify_maintainer_reply  decide if a maintainer merged, closed, or asked
                               something — before you reply

How to write:
  1. Be respectful and non-presumptuous. Short over long. You are a guest.
  2. NEVER call the project or its maintainers "dead", "abandoned", or
     "unmaintained". If you must describe the project's state, say it
     "{pr_templates.REDUCED_MAINTENANCE_PHRASE}".
  3. Never pressure the maintainer. Every offer of help is opt-in and every
     message leaves an explicit no-obligation out.
  4. The PR title must be exactly:
     [Resurrector] Fix: <issue title> (closes #<issue number>)
  5. The PR body must have, in this order: "## What changed", "## Why" (starting
     with "closes #<n>." so GitHub links and closes the issue), "## How to test",
     and "## A note from the contributor" (the fixed co-maintenance offer).
  6. The issue comment is one or two sentences: a PR addressing this is open,
     here is the link, review welcome.
  7. When answering a maintainer's question, answer the specific question FIRST,
     then link the relevant line or diff. No filler, no over-apologizing.

The exact wording of the title, body, and comments is produced for you by the
tools — do not paraphrase around them. Your judgement is for *what* to say
(the one-line "why", the test steps, the answer to a question), not for
re-deriving the fixed template text.
"""


# ---------------------------------------------------------------------------
# Tool adapters (design.md section 3 lists exactly these four)
# ---------------------------------------------------------------------------


@tool
def open_pr(
    repo_full_name: str,
    head_branch: str,
    issue_number: int,
    issue_title: str,
    what_changed: str,
    why: str = "",
    how_to_test: str = "",
    base_branch: str = "",
) -> str:
    """Open the pull request for a fix branch (REQ-4.1, REQ-4.2).

    The title and body follow github-conventions.md exactly and are generated
    for you; you supply the substance (what changed, a one-line why, how to
    test). If a PR for this branch already exists it is reused, not duplicated.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        head_branch: The fix branch the Engineer pushed (``resurrector/fix-issue-{N}``).
        issue_number: The issue this PR closes.
        issue_title: The issue's title, used verbatim in the PR title.
        what_changed: A concise description of the code change.
        why: A one-line restatement of the issue in the maintainer's terms.
        how_to_test: Exact commands or steps a maintainer can run to verify.
        base_branch: Base branch for the PR. Empty means the repo default branch.

    Returns:
        A JSON object with the PR number, URL, issue number, opened_at timestamp,
        branch, and whether this call created the PR (vs reused an existing one).
    """
    try:
        result = github_comms.open_pr(
            repo_full_name,
            head_branch=head_branch,
            issue_number=int(issue_number),
            issue_title=issue_title,
            what_changed=what_changed,
            why=why or None,
            how_to_test=how_to_test or None,
            base_branch=base_branch or None,
        )
    except Exception as exc:  # noqa: BLE001 - surface as text for the model
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return result.to_json()


@tool
def post_issue_comment(repo_full_name: str, issue_number: int, pr_url: str) -> str:
    """Comment on the original issue to announce the PR (REQ-4.3).

    The one-to-two sentence wording is generated from the PR URL per
    github-conventions.md.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        issue_number: The issue to comment on.
        pr_url: The URL of the PR that addresses the issue.

    Returns:
        A JSON object with the posted comment's URL and id.
    """
    body = pr_templates.format_issue_comment(pr_url)
    try:
        result = github_comms.post_issue_comment(
            repo_full_name, int(issue_number), body
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return result.to_json()


@tool
def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> str:
    """Reply on the PR conversation (REQ-5.3).

    Use this to answer a maintainer's question. Answer the specific question
    first, then link the relevant line or diff — keep it short.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        pr_number: The PR to comment on.
        body: The comment text.

    Returns:
        A JSON object with the posted comment's URL and id.
    """
    try:
        result = github_comms.post_pr_comment(repo_full_name, int(pr_number), body)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return result.to_json()


@tool
def classify_maintainer_reply(
    text: str = "", merged: bool = False, state: str = ""
) -> str:
    """Classify a maintainer's activity before replying (REQ-5.3, design.md §5).

    Returns one of ``merged | question | closed | comment | other`` using a
    deterministic, model-free rule: a structured merge/close signal always wins
    over the comment text; otherwise a question mark or interrogative wording
    means ``question``.

    Args:
        text: The maintainer's comment text, if any.
        merged: True when the PR was merged.
        state: The PR state, e.g. ``"open"`` or ``"closed"``.

    Returns:
        A JSON object with the classification, a confidence, and which signal
        drove the decision.
    """
    result = pr_templates.classify_maintainer_reply(
        text=text or None,
        merged=True if merged else None,
        state=state or None,
    )
    return json.dumps(result.to_dict())


#: The tool set from design.md section 3, in the order it is documented there.
COMMUNICATOR_TOOLS = (
    open_pr,
    post_issue_comment,
    post_pr_comment,
    classify_maintainer_reply,
)


# ---------------------------------------------------------------------------
# Orchestrating seams (design.md section 5)
# ---------------------------------------------------------------------------


def announce_pr(
    repo_full_name: str,
    *,
    head_branch: str,
    issue_number: int,
    issue_title: str,
    what_changed: str,
    why: Optional[str] = None,
    how_to_test: Optional[str] = None,
    base_branch: Optional[str] = None,
    repo: Any = None,
    client: Any = None,
    token: Optional[str] = None,
    now: Optional[Callable[..., Any]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[PROpenResult, Optional[CommentResult]]:
    """The REQ-4 happy path: open the PR, then announce it on the issue.

    design.md section 5: ``Communicator(repo, branch)`` opens the PR *and*
    comments on the issue, then the Orchestrator records ``pr_opened``. This is
    the single call the Orchestrator (Task 8) makes for that step; it returns
    both results so the Orchestrator has everything REQ-4.4 needs to persist.

    The issue comment is only posted when the PR actually opened. If opening the
    PR failed, the second result is ``None`` and the failure is carried on the
    first result's ``reason`` — never post "here is the PR" when there is no PR.

    Every GitHub object / client / clock is injectable, so this runs fully
    offline in tests.
    """
    pr_result = github_comms.open_pr(
        repo_full_name,
        head_branch=head_branch,
        issue_number=int(issue_number),
        issue_title=issue_title,
        what_changed=what_changed,
        why=why,
        how_to_test=how_to_test,
        base_branch=base_branch,
        repo=repo,
        client=client,
        token=token,
        now=now,
        sleep=sleep,
    )
    if not pr_result.success or not pr_result.pr_url:
        return pr_result, None

    comment_body = pr_templates.format_issue_comment(pr_result.pr_url)
    comment_result = github_comms.post_issue_comment(
        repo_full_name,
        int(issue_number),
        comment_body,
        repo=repo,
        client=client,
        token=token,
        sleep=sleep,
    )
    return pr_result, comment_result


def reply_to_maintainer(
    repo_full_name: str,
    pr_number: int,
    *,
    text: Optional[str] = None,
    merged: Optional[bool] = None,
    state: Optional[str] = None,
    reply_body: Optional[str] = None,
    run_model: Optional[Callable[[str], str]] = None,
    agent: Any = None,
    repo: Any = None,
    client: Any = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ReplyClassification, Optional[CommentResult]]:
    """The REQ-5.3 reply path: classify the maintainer's activity, reply if asked.

    First classify with the deterministic, model-free
    :func:`src.tools.pr_templates.classify_maintainer_reply` (design.md section
    10). Only a ``question`` warrants a reply here; ``merged`` / ``closed`` /
    ``comment`` are the Orchestrator's to act on (state changes, stopping
    follow-up) and return ``None`` for the comment.

    For a question, the reply body is either supplied (``reply_body`` — a model
    may have composed a specific answer) or the model is asked to draft one via
    ``run_model`` / ``agent``. The **decision** to reply never depends on a
    model; only the wording of the answer may.
    """
    classification = pr_templates.classify_maintainer_reply(
        text=text, merged=merged, state=state
    )
    if classification.classification != "question":
        return classification, None

    body = (reply_body or "").strip()
    if not body:
        runner = run_model
        if runner is None and agent is not None:
            runner = _model_runner(agent)
        if runner is None and STRANDS_AVAILABLE:
            try:
                # Lazy import keeps the model-provider seam off the injected paths.
                from src.tools.model_provider import get_default_model

                runner = _model_runner(build_communicator(model=get_default_model()))
            except Exception:  # noqa: BLE001 - degrade to the canned reply, never crash
                LOGGER.warning("could not build the Communicator agent", exc_info=True)
                runner = None
        if runner is not None:
            prompt = _build_reply_prompt(repo_full_name, pr_number, text or "")
            try:
                body = str(runner(prompt)).strip()
            except Exception as exc:  # noqa: BLE001 - degrade, never crash
                LOGGER.warning("reply drafting failed: %s", exc)
                body = ""
    if not body:
        # No model and no supplied answer: still acknowledge helpfully rather
        # than posting nothing. Kept short and non-presumptuous per the tone
        # rules; the Orchestrator can override with a specific answer.
        body = (
            "Thanks for taking a look — happy to clarify anything about this "
            "change. What would be most helpful?"
        )

    comment = github_comms.post_pr_comment(
        repo_full_name,
        int(pr_number),
        body,
        repo=repo,
        client=client,
        token=token,
        sleep=sleep,
    )
    return classification, comment


def post_follow_up(
    repo_full_name: str,
    pr_number: int,
    *,
    stage: int = 7,
    repo: Any = None,
    client: Any = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CommentResult:
    """Post the single, warm follow-up nudge on a PR (REQ-6.2 / github-conventions.md).

    The Communicator is the only agent that posts comments (design.md section
    3), so the Orchestrator's timed escalation routes the follow-up through
    here rather than touching :mod:`src.tools.github_comms` itself. The *timing*
    and the "never more than one 7-day and one 14-day nudge" rule are the
    Orchestrator/Processor's job (Task 8/9); this only supplies the wording
    (:func:`src.tools.pr_templates.follow_up_comment`) and carries it to GitHub.

    ``stage`` (7 or 14) is passed through for symmetry, though the copy is the
    same warm one-liner for both. Every GitHub object / client / clock is
    injectable, so this runs fully offline in tests.
    """
    body = pr_templates.follow_up_comment(stage)
    return github_comms.post_pr_comment(
        repo_full_name,
        int(pr_number),
        body,
        repo=repo,
        client=client,
        token=token,
        sleep=sleep,
    )


def _build_reply_prompt(repo_full_name: str, pr_number: int, question: str) -> str:
    """Render a maintainer's question into a drafting prompt for the model."""
    return (
        f"A maintainer of {repo_full_name} asked this on PR #{pr_number}:\n\n"
        f"{question}\n\n"
        "Draft a short, technically precise reply. Answer the specific question "
        "first, then point to the relevant change. Do not over-apologize, do not "
        "pad, and do not pressure them. Reply with only the comment text."
    )


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def build_communicator(
    *,
    model: Any = None,
    tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """Construct the Communicator ``strands.Agent``.

    ``model`` is passed through so the Orchestrator (or a test) can pin a
    specific Bedrock model; ``None`` lets strands apply its configured default.

    Raises:
        StrandsUnavailableError: when ``strands`` is not installed. The
            deterministic seams (:func:`announce_pr`, :func:`reply_to_maintainer`
            with a supplied ``reply_body``) still work without a model.
    """
    if not STRANDS_AVAILABLE or _StrandsAgent is None:
        raise StrandsUnavailableError(
            "strands-agents is not installed; the model-backed Communicator is "
            "unavailable. announce_pr(...) and the template functions still work."
        )
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt or COMMUNICATOR_SYSTEM_PROMPT,
        "tools": list(tools) if tools is not None else list(COMMUNICATOR_TOOLS),
    }
    if model is not None:
        kwargs["model"] = model
    return _StrandsAgent(**kwargs)


def _model_runner(agent: Any) -> Callable[[str], str]:
    """Adapt a strands ``Agent`` to the ``Callable[[str], str]`` shape.

    Same convention as the Analyst and Engineer, verified against strands-agents
    1.55.1: ``Agent.__call__(prompt)`` returns an ``AgentResult`` whose
    ``__str__`` yields the model's text, so ``str(agent(prompt))`` is the correct
    text extraction. The live Bedrock round-trip behind it is the one thing no
    offline test can cover.
    """

    def run(prompt: str) -> str:
        return str(agent(prompt))

    return run


# ---------------------------------------------------------------------------
# Agent-as-tool entry point (design.md section 8)
# ---------------------------------------------------------------------------


@tool
def communicator_agent(
    repo_full_name: str,
    head_branch: str,
    issue_number: int,
    issue_title: str,
    what_changed: str,
    why: str = "",
    how_to_test: str = "",
) -> str:
    """Open the PR and announce it on the issue for a ready fix branch. Returns JSON.

    This is the Orchestrator's handle on the Communicator's happy path (design.md
    sections 5 and 8): it opens the PR (REQ-4.1/4.2) and comments on the issue
    (REQ-4.3) in one call, and returns the PR fields the Orchestrator persists as
    ``pr_opened`` (REQ-4.4).

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        head_branch: The fix branch the Engineer pushed.
        issue_number: The issue the PR closes.
        issue_title: The issue's title.
        what_changed: A concise description of the code change.
        why: A one-line restatement of the issue.
        how_to_test: Steps a maintainer can run to verify.

    Returns:
        A JSON object with the PR result and the issue-comment result.
    """
    pr_result, comment_result = announce_pr(
        repo_full_name,
        head_branch=head_branch,
        issue_number=int(issue_number),
        issue_title=issue_title,
        what_changed=what_changed,
        why=why or None,
        how_to_test=how_to_test or None,
    )
    return json.dumps(
        {
            "pr": pr_result.to_dict(),
            "issue_comment": comment_result.to_dict() if comment_result else None,
        }
    )
