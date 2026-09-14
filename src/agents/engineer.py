"""Engineer sub-agent — creates the fix branch, authors the fix, pushes it.

The Engineer is the second stage of the Orchestrator's pipeline (design.md
sections 3 and 5) and the **only** agent that writes code or creates branches.
It runs when the Analyst scored an issue ``trivial`` or ``moderate``:

- REQ-3.1 create ``resurrector/fix-issue-{N}`` from the repo's default branch
- REQ-3.2 write the file changes and, **where tests exist**, run them and
  require they pass before proceeding
- REQ-3.3 if the fix cannot be made syntactically valid, or the tests fail,
  abort and recommend ``status = fix_failed`` — no PR
- REQ-3.4 when the fix is valid, push the commit(s) to the fix branch via the
  GitHub API

It does **not** open pull requests and does **not** post comments. Those belong
to the Communicator (Task 7), which is the only agent permitted to do either
(design.md section 3). A test asserts this module's source contains no
pull-request or issue-comment API call, so the boundary is checked rather than
merely documented.

Layering
--------
All business logic lives in framework-free modules, exactly as the Analyst does
it:

- :mod:`src.tools.github_write` — branch refs, file reads-for-edit, the Contents
  API write, the atomic trees-API push, and syntax validation
- :mod:`src.tools.suite_runner` — test-runner detection and the guarded
  execution of a third-party suite

This module holds only the ``strands`` wiring: the five ``@tool`` adapters from
design.md section 3, the system prompt, :func:`build_engineer`, the orchestrating
:func:`implement_fix`, and the agent-as-tool entry point :func:`engineer_agent`
(design.md section 8).

Security posture — read :mod:`src.tools.suite_runner` before enabling anything
------------------------------------------------------------------------------
``run_tests`` executes a test suite from a repository we do not control. That is
arbitrary remote code execution, and it is off by default
(``RESURRECTOR_ALLOW_TEST_EXECUTION``). The full threat model, the controls, the
residual risk, and what ``strands.Agent(sandbox=...)`` can and cannot do about it
are documented in that module's docstring. The short version: with execution
disabled — the default — the Engineer still creates branches, validates syntax,
and pushes commits; it simply reports ``not_executed`` instead of a test verdict,
and that is never treated as a pass.

Deadline (NFR cost)
-------------------
The Lambda timeout is ≤ 15 minutes and the Engineer must finish within 10
(``RESURRECTOR_ENGINEER_TIMEOUT_SECONDS``, default 600). :func:`implement_fix`
checks the deadline before each expensive step and before the push, and aborts
with ``fix_failed`` rather than being killed mid-push by the runtime.

REQ-3.3 boundary
----------------
REQ-3.3 says to "record ``status = fix_failed``", but the Orchestrator is the
only component that writes DynamoDB state (design.md section 3). So the Engineer
**returns** the decision: :attr:`EngineerResult.recommended_status` carries
``fix_failed`` for the Orchestrator (Task 8) to persist. Nothing here imports the
state layer, and a test asserts that.

Verification status
-------------------
Everything below is exercised offline with fake PyGithub objects and a fake
subprocess runner. Two things cannot be verified without live credentials and
are honestly unverified here: the Bedrock round-trip behind ``agent(prompt)``,
and a real branch/commit landing on github.com. Task 13 covers both against a
private test repo.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from src.tools import github_write, suite_runner
from src.tools.complexity_scorer import iter_json_objects
from src.tools.github_write import (
    BranchRef,
    CommitResult,
    FileChange,
    PathTraversalError,
    SyntaxCheck,
)
from src.tools.suite_runner import TestRunResult

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded strands import
# ---------------------------------------------------------------------------

# strands is a core dependency: the Engineer *is* a strands.Agent (design.md
# sections 3 and 8). The guard mirrors src.agents.analyst — it keeps a Lambda
# cold start or a stripped CI image from hard-failing at import time, and it
# keeps the deterministic path (a caller-supplied change set) working when the
# model layer is unavailable. It is not an invitation to run without strands.
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

    Same identity fallback as the Analyst: without ``strands`` the adapters stay
    ordinary callables, so the module imports and unit-tests cleanly; with
    ``strands`` the real decorator produces proper tool specs.
    """
    if _strands_tool is not None:
        return _strands_tool(func)
    return func


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Recommended DynamoDB status when a fix cannot be completed (REQ-3.3). Matches
#: the ``fix_failed`` member of ``dynamo_tools.VALID_STATUSES``; declared as a
#: literal so this module never imports the state layer (design.md section 3).
FIX_FAILED_STATUS = "fix_failed"

#: NFR cost: "Engineer must complete within 10 min", inside the ≤15 min Lambda
#: timeout. Configurable so a slower suite can be given more room deliberately
#: rather than by accident.
DEFAULT_ENGINEER_TIMEOUT_SECONDS = 600
ENGINEER_TIMEOUT_ENV_VAR = "RESURRECTOR_ENGINEER_TIMEOUT_SECONDS"

#: Characters of an existing file included in the fix prompt.
FILE_PROMPT_CHARS = 8000
#: Characters of the issue body included in the fix prompt.
ISSUE_BODY_PROMPT_CHARS = 6000


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid.

    Mirrors the ``_env_*`` convention used across the tool modules.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def engineer_timeout(timeout: Optional[int] = None) -> int:
    """Resolve the overall Engineer deadline in seconds (default 600)."""
    if timeout is not None:
        return int(timeout)
    return _env_int(ENGINEER_TIMEOUT_ENV_VAR, DEFAULT_ENGINEER_TIMEOUT_SECONDS)


class DeadlineExceeded(RuntimeError):
    """Raised internally when the Engineer's overall deadline passes."""


# ---------------------------------------------------------------------------
# System prompt (design.md section 3 + NFR safety)
# ---------------------------------------------------------------------------

#: The Engineer's instructions. Wording follows the NFR safety rule: never
#: assert a repository is "dead", never overreach beyond the scoped issue.
ENGINEER_SYSTEM_PROMPT = """\
You are the Engineer in an autonomous open-source contribution pipeline.

An Analyst has already read a repository that appears to have reduced
maintenance activity, chosen one open issue, and judged it tractable. Your job
is to write the smallest correct fix for that issue and get it onto a branch.

You may use these tools:
  - create_branch  create the fix branch off the default branch
  - get_file       read a file at a ref, with the blob SHA needed to update it
  - write_file     create or update a single file on the fix branch
  - run_tests      run the repository's own test suite, if it has one
  - push_commit    commit the whole change set to the fix branch at once

You do NOT open pull requests and you do NOT post comments. Another agent does
that after you succeed. Do not try.

How to work:
  1. Read before you write. Fetch every file you intend to change and quote the
     surrounding code back to yourself before editing it. Never guess at the
     contents of a file you have not read.
  2. Change as little as possible. The smallest diff that fixes the reported
     failure is the right diff. Do not reformat, do not rename, do not upgrade
     dependencies, do not "clean up while you are here".
  3. Stay inside the issue. If fixing it properly needs a change the issue did
     not ask for, stop and say so instead of widening the scope.
  4. Preserve the project's conventions - its naming, its error handling, its
     test style. You are a guest in this codebase.
  5. Add or extend a test that fails before your change and passes after it,
     when the repository has a test suite to put it in.
  6. Never write secrets, credentials, tokens, or telemetry into the repository.
     Never modify CI configuration or workflow files.

When you are asked for a change set, reply with ONLY this JSON object and no
other text:

{
  "summary": "one sentence on what you changed and why",
  "commit_message": "imperative, under 72 characters",
  "files": [
    {"path": "src/thing.py", "content": "<the COMPLETE new file contents>"}
  ],
  "confident": true
}

Rules for your answer:
  - "content" must be the entire file after your change, not a diff and not a
    fragment. It replaces the file wholesale.
  - Paths are repository-relative. No leading slash, no "..", never a path
    outside the repository.
  - Every file you list must be one you have actually read, or a genuinely new
    file the fix requires.
  - Set "confident" to false if you could not write a fix you would stand
    behind. An honest refusal is a useful answer; a plausible-looking wrong
    patch costs a maintainer their afternoon.
  - Describe the repository as appearing to have reduced maintenance activity.
    Never describe a project or its maintainers as dead or abandoned.
"""


# ---------------------------------------------------------------------------
# Tool adapters (design.md section 3 lists exactly these five)
# ---------------------------------------------------------------------------


@tool
def create_branch(repo_full_name: str, issue_number: int) -> str:
    """Create the fix branch for an issue, off the repository's default branch.

    The branch is always named ``resurrector/fix-issue-{issue_number}``. If it
    already exists it is reused rather than treated as an error.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        issue_number: The issue this fix targets.

    Returns:
        A JSON object with the branch name, its head SHA, the base branch it was
        cut from, and whether this call created it.
    """
    try:
        branch = github_write.create_branch(repo_full_name, int(issue_number))
    except Exception as exc:  # noqa: BLE001 - surface as text for the model
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return json.dumps(branch.to_dict())


@tool
def get_file(repo_full_name: str, path: str, ref: str = "") -> str:
    """Read one file for editing: its text plus the blob SHA needed to update it.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        path: Repository-relative path to the file.
        ref: Branch, tag, or commit SHA. Empty means the default branch.

    Returns:
        A JSON object with the text, the blob SHA, whether the file exists, and
        whether the text was truncated by the size cap.
    """
    try:
        content = github_write.get_file(repo_full_name, path, ref=ref or None)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return json.dumps(content.to_dict())


@tool
def write_file(
    repo_full_name: str,
    path: str,
    content: str,
    branch: str,
    message: str,
    sha: str = "",
) -> str:
    """Create or update a single file on the fix branch.

    Prefer ``push_commit`` when a fix touches more than one file — it lands them
    as one commit instead of several.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        path: Repository-relative path. Paths that escape the repository root are
            refused.
        content: The complete new contents of the file.
        branch: The fix branch. Required — never the default branch.
        message: Commit message.
        sha: The existing blob SHA when updating a file. Empty when creating one.

    Returns:
        A JSON object with the resulting commit SHA and the file written.
    """
    try:
        result = github_write.write_file(
            repo_full_name,
            path,
            content,
            branch=branch,
            message=message,
            sha=sha or None,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return json.dumps(result.to_dict())


@tool
def run_tests(workspace: str, command: str = "") -> str:
    """Run the repository's own test suite in a prepared local workspace.

    Execution of third-party test code is disabled unless the operator opted in.
    When it is disabled this returns ``outcome = "not_executed"``, which is *not*
    a pass. A repository with no suite returns ``outcome = "no_tests_found"``,
    which does not block the fix.

    Args:
        workspace: Path to the local checkout to run in.
        command: Optional runner command, space-separated. It is checked against
            an allowlist of known test runners and refused if it does not match.
            Leave empty to infer the runner from the repository's own markers.

    Returns:
        A JSON object with the outcome, whether it blocks the fix, the exit code,
        and captured output.
    """
    argv = command.split() if command.strip() else None
    result = suite_runner.run_tests(workspace, command=argv)
    return json.dumps(result.to_dict())


@tool
def push_commit(
    repo_full_name: str, branch: str, message: str, files_json: str
) -> str:
    """Commit a whole change set to the fix branch as a single commit.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        branch: The fix branch to commit on.
        message: Commit message.
        files_json: A JSON array of ``{"path": ..., "content": ...}`` objects,
            each carrying the complete new contents of one file.

    Returns:
        A JSON object with the commit SHA and the files it contains.
    """
    try:
        payload = json.loads(files_json)
    except ValueError as exc:
        return json.dumps({"error": f"files_json is not valid JSON: {exc}"})
    if not isinstance(payload, list):
        return json.dumps({"error": "files_json must be a JSON array"})
    try:
        result = github_write.push_commit(
            repo_full_name, payload, branch=branch, message=message
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"})
    return json.dumps(result.to_dict())


#: The tool set from design.md section 3, in the order it is documented there.
ENGINEER_TOOLS = (
    create_branch,
    get_file,
    write_file,
    run_tests,
    push_commit,
)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class EngineerResult:
    """What the Engineer hands back to the Orchestrator.

    ``recommended_status`` is the REQ-3.3 decision: :data:`FIX_FAILED_STATUS` on
    failure, ``None`` on success (a successful fix is not itself the terminal
    state — the Communicator opens the PR and the Orchestrator records
    ``pr_opened``). The Engineer never persists it.

    ``test_result`` is the full :class:`~src.tools.suite_runner.TestRunResult`
    dict, not a boolean, so the Communicator can be accurate in the PR body about
    whether the suite passed, did not exist, or was not run.
    """

    repo_full_name: str
    issue_number: int
    success: bool
    reason: str
    branch: Optional[str] = None
    base_branch: Optional[str] = None
    branch_created: Optional[bool] = None
    commit_shas: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    test_result: Optional[dict[str, Any]] = None
    syntax_checks: list[dict[str, Any]] = field(default_factory=list)
    recommended_status: Optional[str] = None
    notes: str = ""
    elapsed_seconds: Optional[float] = None
    source: str = "model"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "issue_number": self.issue_number,
            "success": self.success,
            "reason": self.reason,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "branch_created": self.branch_created,
            "commit_shas": list(self.commit_shas),
            "files_changed": list(self.files_changed),
            "test_result": self.test_result,
            "syntax_checks": list(self.syntax_checks),
            "recommended_status": self.recommended_status,
            "notes": self.notes,
            "elapsed_seconds": self.elapsed_seconds,
            "source": self.source,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string — the agent-as-tool return shape."""
        return json.dumps(self.to_dict(), **kwargs)


@dataclass
class FixPlan:
    """The model's proposed change set, parsed and validated."""

    changes: list[FileChange]
    summary: str = ""
    commit_message: str = ""
    confident: bool = True

    def paths(self) -> list[str]:
        """Just the paths being changed."""
        return [change.path for change in self.changes]


# ---------------------------------------------------------------------------
# Model output parsing
# ---------------------------------------------------------------------------


def parse_fix_plan(text: Any) -> Optional[FixPlan]:
    """Recover a :class:`FixPlan` from raw model text, or ``None``.

    Accepts bare JSON, JSON inside a ```` ```json ```` fence, and JSON wrapped in
    prose — the same three shapes the Analyst has to cope with, handled by the
    same string-aware brace scanner
    (:func:`src.tools.complexity_scorer.iter_json_objects`) rather than a second
    copy of it.

    Returns ``None`` — never raises — when nothing usable can be recovered:
    no ``files`` array, an entry missing ``path``/``content``, a path that
    escapes the repository root, or a change set over the configured caps.
    A ``None`` here becomes a ``fix_failed`` recommendation, which is the correct
    outcome for a model that could not produce a change set (REQ-3.3).
    """
    if not isinstance(text, str) or not text.strip():
        return None

    for candidate in iter_json_objects(text):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(decoded, dict):
            continue
        raw_files = decoded.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            continue
        try:
            changes = github_write.coerce_changes(raw_files)
        except (ValueError, TypeError, PathTraversalError) as exc:
            LOGGER.warning("rejected the model's change set: %s", exc)
            continue
        confident = decoded.get("confident", True)
        return FixPlan(
            changes=changes,
            summary=str(decoded.get("summary") or "").strip(),
            commit_message=str(decoded.get("commit_message") or "").strip(),
            confident=confident is not False,
        )
    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _clip(text: Optional[str], limit: int) -> str:
    """Clip ``text`` to ``limit`` characters with an explicit marker."""
    if not text:
        return "(none)"
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[clipped at {limit} characters]"


def build_fix_prompt(
    *,
    repo_full_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: Optional[str],
    approach: str,
    branch: str,
    files: Sequence[Any],
) -> str:
    """Render the per-issue fix task for the model.

    ``files`` are :class:`~src.tools.github_write.FileContent` records the caller
    already fetched (the Analyst's ``files_affected`` is where they come from).
    Handing the model the code up front rather than making it spend tool calls
    re-reading is both cheaper and one fewer chance to hallucinate a path. Every
    section is length-capped.
    """
    file_blocks = []
    for item in files:
        path = getattr(item, "path", None) or ""
        text = getattr(item, "text", None)
        exists = getattr(item, "exists", False)
        if not exists or text is None:
            file_blocks.append(f"--- {path} (does not exist yet) ---")
            continue
        file_blocks.append(
            f"--- {path} ---\n{_clip(text, FILE_PROMPT_CHARS)}\n--- end {path} ---"
        )

    return f"""\
Repository: {repo_full_name}
Fix branch (already created): {branch}

This repository appears to have reduced maintenance activity.

Issue #{issue_number}: {issue_title}

Issue body:
{_clip(issue_body, ISSUE_BODY_PROMPT_CHARS)}

The Analyst's suggested approach:
{approach or "(none supplied)"}

Current contents of the files the Analyst implicated:
{chr(10).join(file_blocks) or "(no files were fetched)"}

Write the smallest correct fix. Reply with only the JSON object.
"""


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def build_engineer(
    *,
    model: Any = None,
    tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """Construct the Engineer ``strands.Agent``.

    ``model`` is passed through so the Orchestrator (or a test) can pin a
    specific Bedrock model; ``None`` lets strands apply its configured default.

    On ``sandbox=``: strands-agents 1.55.1 does accept a ``sandbox`` argument and
    ships ``DockerSandbox`` / ``SshSandbox`` backends. We deliberately do not
    pass one — it would not constrain :func:`run_tests`, which calls
    :func:`subprocess.run` in-process and so never goes through the sandbox API,
    and neither backend is available inside Lambda. See the
    :mod:`src.tools.suite_runner` docstring for the full finding.

    Raises:
        StrandsUnavailableError: when ``strands`` is not installed. Callers that
            need to keep working regardless should call :func:`implement_fix`
            with an explicit ``changes`` set, which needs no model.
    """
    if not STRANDS_AVAILABLE or _StrandsAgent is None:
        raise StrandsUnavailableError(
            "strands-agents is not installed; the model-backed Engineer is "
            "unavailable. implement_fix(changes=[...]) still works without it."
        )
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt or ENGINEER_SYSTEM_PROMPT,
        "tools": list(tools) if tools is not None else list(ENGINEER_TOOLS),
    }
    if model is not None:
        kwargs["model"] = model
    return _StrandsAgent(**kwargs)


def _model_runner(agent: Any) -> Callable[[str], str]:
    """Adapt a strands ``Agent`` to the ``Callable[[str], str]`` shape.

    Same convention as the Analyst, verified against the installed
    strands-agents 1.55.1: ``Agent.__call__(prompt)`` returns an
    ``AgentResult`` whose ``__str__`` yields the model's text, so
    ``str(agent(prompt))`` is the correct text extraction. The live Bedrock
    round-trip behind it is the one thing no offline test can cover; every other
    path injects ``run_model`` or a fake ``agent``.
    """

    def run(prompt: str) -> str:
        return str(agent(prompt))

    return run


# ---------------------------------------------------------------------------
# implement_fix (REQ-3.1 .. REQ-3.4)
# ---------------------------------------------------------------------------


def _fail(
    *,
    repo_full_name: str,
    issue_number: int,
    reason: str,
    started: float,
    now: Callable[[], float],
    branch: Optional[BranchRef] = None,
    test_result: Optional[TestRunResult] = None,
    syntax_checks: Optional[Sequence[SyntaxCheck]] = None,
    files_changed: Optional[Sequence[str]] = None,
    source: str = "model",
    notes: str = "",
) -> EngineerResult:
    """Build the REQ-3.3 failure result: no push, ``fix_failed`` recommended."""
    return EngineerResult(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        success=False,
        reason=reason,
        branch=branch.name if branch else None,
        base_branch=branch.base_branch if branch else None,
        branch_created=branch.created if branch else None,
        commit_shas=[],
        files_changed=list(files_changed or []),
        test_result=test_result.to_dict() if test_result else None,
        syntax_checks=[check.to_dict() for check in (syntax_checks or [])],
        recommended_status=FIX_FAILED_STATUS,
        notes=notes,
        elapsed_seconds=round(now() - started, 3),
        source=source,
    )


def implement_fix(
    repo_full_name: str,
    issue_number: int,
    *,
    issue_title: str = "",
    issue_body: Optional[str] = None,
    approach: str = "",
    files_affected: Optional[Sequence[str]] = None,
    changes: Optional[Sequence[Any]] = None,
    commit_message: Optional[str] = None,
    repo: Any = None,
    client: Any = None,
    token: Optional[str] = None,
    run_model: Optional[Callable[[str], str]] = None,
    agent: Any = None,
    use_model: bool = True,
    base_branch: Optional[str] = None,
    allow_test_execution: Optional[bool] = None,
    runner_process: Optional[Callable[..., Any]] = None,
    workspace: Optional[Any] = None,
    base_files: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EngineerResult:
    """Author and push a fix for one issue. Returns the decision, never persists it.

    Steps, in this order:

    1. **Obtain a change set.** Either the caller supplies ``changes`` (the
       deterministic, model-free path used by tests and by the Orchestrator when
       it already knows the patch), or the model is prompted with the issue and
       the current contents of ``files_affected``. An unparsable reply, or a
       reply with ``confident: false``, is a REQ-3.3 failure.
    2. **Validate syntax (REQ-3.3).** ``.py`` and ``.json`` files are really
       parsed; other languages are marked ``validated=False`` rather than
       pretended over. A parse failure aborts here — *before* the branch is
       created, so a fix we already know is broken never leaves a stray ref
       behind. REQ-3.1's ordering ("create the branch, then author the fix") is
       still honoured for every fix that can succeed; this only moves the cheap,
       API-free check ahead of the API call.
    3. **Create the branch (REQ-3.1).** ``resurrector/fix-issue-{N}`` off the
       default branch head. Idempotent — an existing branch is reused.
    4. **Run the tests (REQ-3.2).** The change set is materialized into a
       workspace under ``/tmp`` on top of ``base_files`` and the repo's own suite
       is run, under every control in :mod:`src.tools.suite_runner`. A failing or
       timed-out suite aborts. "No tests found" and "not executed" do not abort,
       and neither is reported as a pass.
    5. **Push (REQ-3.4).** One atomic commit via the git trees API.

    The overall deadline (default 600s, NFR cost) is checked before each step and
    immediately before the push, so we abort cleanly rather than being killed by
    the Lambda runtime part-way through a commit.

    Injection points, all of which keep this runnable offline: ``changes``
    skips the model, ``run_model`` / ``agent`` supply the model, ``repo`` /
    ``client`` supply GitHub, ``runner_process`` supplies the subprocess,
    ``workspace`` supplies the checkout directory, and ``now`` / ``sleep``
    supply time.
    """
    started = now()
    deadline = engineer_timeout(timeout)

    def remaining() -> float:
        return deadline - (now() - started)

    def check_deadline(step: str) -> None:
        if remaining() <= 0:
            raise DeadlineExceeded(
                f"the {deadline}s Engineer deadline passed before {step}"
            )

    try:
        issue_number = int(issue_number)
    except (TypeError, ValueError):
        return _fail(
            repo_full_name=repo_full_name,
            issue_number=0,
            reason=f"issue_number is not an integer: {issue_number!r}",
            started=started,
            now=now,
            source="none",
        )

    source = "caller" if changes is not None else "model"
    owned_workspace: Optional[Any] = None

    try:
        # -- 1. change set ------------------------------------------------
        check_deadline("authoring the fix")
        if changes is not None:
            try:
                plan = FixPlan(
                    changes=github_write.coerce_changes(changes),
                    summary="change set supplied by the caller",
                    commit_message=commit_message or "",
                )
            except (ValueError, TypeError, PathTraversalError) as exc:
                return _fail(
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    reason=f"the supplied change set was rejected: {exc}",
                    started=started,
                    now=now,
                    source=source,
                )
        else:
            plan = _plan_from_model(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                approach=approach,
                files_affected=files_affected,
                repo=repo,
                client=client,
                token=token,
                run_model=run_model,
                agent=agent,
                use_model=use_model,
                base_branch=base_branch,
                sleep=sleep,
            )
            if plan is None:
                return _fail(
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    reason=(
                        "could not obtain a usable change set from the model; "
                        "no branch was created and nothing was pushed"
                    ),
                    started=started,
                    now=now,
                    source=source,
                )
            if not plan.confident:
                return _fail(
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    reason=(
                        "the model reported it could not write a fix it would "
                        "stand behind (confident=false)"
                    ),
                    started=started,
                    now=now,
                    source=source,
                    notes=plan.summary,
                )

        paths = plan.paths()

        # -- 2. syntax validation (REQ-3.3) ------------------------------
        checks = github_write.validate_changes(plan.changes)
        failures = github_write.syntax_failures(checks)
        if failures:
            detail = "; ".join(f"{c.path}: {c.error}" for c in failures)
            return _fail(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                reason=f"the fix is not syntactically valid: {detail}",
                started=started,
                now=now,
                syntax_checks=checks,
                files_changed=paths,
                source=source,
            )

        # -- 3. branch (REQ-3.1) -----------------------------------------
        check_deadline("creating the fix branch")
        branch = github_write.create_branch(
            repo_full_name,
            issue_number,
            repo=repo,
            client=client,
            token=token,
            base_branch=base_branch,
            sleep=sleep,
        )

        # -- 4. tests (REQ-3.2) ------------------------------------------
        check_deadline("running the tests")
        run_root = workspace
        if run_root is None:
            owned_workspace = suite_runner.create_workspace(
                prefix=f"issue-{issue_number}-"
            )
            run_root = owned_workspace
        if base_files:
            suite_runner.materialize(run_root, base_files)
        suite_runner.materialize(
            run_root, {change.path: change.content for change in plan.changes}
        )

        # Never let the suite outlive the Engineer's own deadline: take the
        # tighter of the configured per-suite timeout and the time we have left.
        suite_timeout = max(1, int(min(remaining(), suite_runner.test_timeout())))
        test_result = suite_runner.run_tests(
            run_root,
            allow_execution=allow_test_execution,
            timeout=suite_timeout,
            runner_process=runner_process,
        )
        if test_result.blocks_progress():
            return _fail(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                reason=f"tests did not pass: {test_result.reason}",
                started=started,
                now=now,
                branch=branch,
                test_result=test_result,
                syntax_checks=checks,
                files_changed=paths,
                source=source,
            )

        # -- 5. push (REQ-3.4) -------------------------------------------
        check_deadline("pushing the commit")
        message = (
            plan.commit_message
            or commit_message
            or f"Fix issue #{issue_number}"
        )
        commit: CommitResult = github_write.push_commit(
            repo_full_name,
            plan.changes,
            branch=branch.name,
            message=message,
            repo=repo,
            client=client,
            token=token,
            sleep=sleep,
        )

        notes = plan.summary
        if not test_result.executed:
            notes = (
                f"{notes} | tests: {test_result.outcome} ({test_result.reason})"
                if notes
                else f"tests: {test_result.outcome} ({test_result.reason})"
            )
        return EngineerResult(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            success=True,
            reason=f"pushed {len(commit.files)} file(s) to {branch.name}",
            branch=branch.name,
            base_branch=branch.base_branch,
            branch_created=branch.created,
            commit_shas=[commit.commit_sha] if commit.commit_sha else [],
            files_changed=list(commit.files),
            test_result=test_result.to_dict(),
            syntax_checks=[check.to_dict() for check in checks],
            recommended_status=None,
            notes=notes,
            elapsed_seconds=round(now() - started, 3),
            source=source,
        )

    except DeadlineExceeded as exc:
        LOGGER.warning("Engineer deadline exceeded: %s", exc)
        return _fail(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            reason=str(exc),
            started=started,
            now=now,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001 - design.md section 10: return, don't crash
        LOGGER.warning("Engineer failed on %s", repo_full_name, exc_info=True)
        return _fail(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            reason=f"{exc.__class__.__name__}: {exc}",
            started=started,
            now=now,
            source=source,
        )
    finally:
        if owned_workspace is not None:
            suite_runner.cleanup_workspace(owned_workspace)


def _plan_from_model(
    *,
    repo_full_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: Optional[str],
    approach: str,
    files_affected: Optional[Sequence[str]],
    repo: Any,
    client: Any,
    token: Optional[str],
    run_model: Optional[Callable[[str], str]],
    agent: Any,
    use_model: bool,
    base_branch: Optional[str],
    sleep: Callable[[float], None],
) -> Optional[FixPlan]:
    """Prompt the model for a change set, returning ``None`` when it cannot.

    Reads each ``files_affected`` path first so the prompt carries real code
    rather than the model's memory of it. A read failure is logged and the file
    is skipped — a path the Analyst invented must not abort the run, it just
    means the model sees "does not exist yet".
    """
    runner = run_model
    if runner is None and agent is not None:
        runner = _model_runner(agent)
    if runner is None and use_model and STRANDS_AVAILABLE:
        try:
            # Lazy import keeps the model-provider seam off the injected paths.
            from src.tools.model_provider import get_default_model

            runner = _model_runner(build_engineer(model=get_default_model()))
        except Exception:  # noqa: BLE001
            LOGGER.warning("could not build the Engineer agent", exc_info=True)
            runner = None
    if runner is None:
        LOGGER.warning("no model backend available; the Engineer cannot author a fix")
        return None

    fetched = []
    for path in files_affected or []:
        try:
            fetched.append(
                github_write.get_file(
                    repo_full_name,
                    path,
                    ref=base_branch,
                    repo=repo,
                    client=client,
                    token=token,
                    sleep=sleep,
                )
            )
        except Exception:  # noqa: BLE001 - a bad path must not abort the fix
            LOGGER.info("could not read %s from %s", path, repo_full_name)

    prompt = build_fix_prompt(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        approach=approach,
        branch=github_write.fix_branch_name(issue_number),
        files=fetched,
    )
    try:
        raw = runner(prompt)
    except Exception as exc:  # noqa: BLE001 - Bedrock throttling, transport, etc.
        LOGGER.warning("Engineer model call failed: %s", exc)
        return None
    return parse_fix_plan(raw)


# ---------------------------------------------------------------------------
# Agent-as-tool entry point (design.md section 8)
# ---------------------------------------------------------------------------


@tool
def engineer_agent(
    repo_full_name: str,
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    approach: str = "",
    files_affected: str = "",
) -> str:
    """Write a fix for one issue, validate it, and push it to a fix branch.

    This is the Orchestrator's handle on the Engineer (design.md section 8). It
    creates the branch, authors and validates the change, runs the repository's
    tests where they exist, and pushes. It does not open a pull request.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        issue_number: The issue to fix.
        issue_title: The issue title.
        issue_body: The issue body.
        approach: The Analyst's suggested plan.
        files_affected: Comma-separated paths the Analyst implicated.

    Returns:
        A JSON object with success, the branch, the commit SHA(s), the files
        changed, the test outcome, and — on failure — the recommended status
        ``fix_failed`` for the Orchestrator to record.
    """
    paths = [p.strip() for p in (files_affected or "").split(",") if p.strip()]
    return implement_fix(
        repo_full_name,
        issue_number,
        issue_title=issue_title,
        issue_body=issue_body or None,
        approach=approach,
        files_affected=paths,
    ).to_json()
