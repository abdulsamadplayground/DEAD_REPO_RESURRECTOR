"""Analyst sub-agent — reads a repo, picks a target issue, scores complexity.

The Analyst is the first stage of the Orchestrator's pipeline (design.md
sections 3 and 5). It is **strictly read-only** against GitHub: it reads the
open issues, README, primary language, folder structure, and last 10 commits
(REQ-2.1), scores the top-voted issue as ``trivial | moderate | complex`` with a
confidence and a plan (REQ-2.2), and returns the gating decision for REQ-2.3.

Layering
--------
All business logic lives in framework-free modules:

- :mod:`src.tools.github_tools` — the GitHub reads
- :mod:`src.tools.complexity_scorer` — parsing, the deterministic heuristic,
  and the gate

This module holds only the ``strands`` wiring: four ``@tool`` adapters, the
system prompt, :func:`build_analyst`, and the agent-as-tool entry point
:func:`analyst_agent` (design.md section 8).

Strands is the primary path
----------------------------
``strands`` is the **core** of this system (design.md sections 3 and 8): the
Analyst *is* a ``strands.Agent``. In the normal, fully-provisioned environment
``strands`` is installed and :func:`analyze_repo` (``use_model=True``, the
default) builds a real agent and drives the model. The deterministic
:mod:`src.tools.complexity_scorer` is **not** the default execution path — it is
the design.md section 10 *fallback*, used only when (a) Bedrock is unavailable
or throttled, or (b) the model returns output that cannot be parsed as the
required JSON.

The ``strands`` import is nonetheless *guarded*. This is deliberate defensive
engineering, not an invitation to run without the framework: it keeps a Lambda
cold-start or a stripped CI image from hard-failing at import time, and it lets
the deterministic fallback keep producing a verdict when the model layer is
down. Importing this module — or calling :func:`analyze_repo` — therefore works
whether or not ``strands`` is present; only :func:`build_analyst` and an actual
model round-trip require it.

Verified against strands-agents 1.55.1 (see :func:`_model_runner`): calling an
``Agent`` instance returns a ``strands.agent.agent_result.AgentResult`` whose
``__str__`` concatenates the text blocks of the final message, so
``str(agent(prompt))`` yields the model's text. The one piece that cannot be
exercised without live AWS/Bedrock credentials is that real round-trip; the
``run_model`` / ``agent`` injection seam keeps every other path testable
offline, and Task 13 covers the live invocation.

REQ-2.3 boundary
----------------
The Analyst **returns** the gating decision; it never writes DynamoDB. The
Orchestrator (Task 8) is the only component that persists state (design.md
section 3), and it reads :attr:`AnalystReport.gate` to decide whether to record
``skipped_complex`` or continue to the Engineer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.tools import github_tools
from src.tools.complexity_scorer import (
    ComplexityResult,
    GateDecision,
    gate_decision,
    parse_model_output,
    score_issue,
)
from src.tools.github_tools import IssueSummary, RepoContext

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded strands import
# ---------------------------------------------------------------------------

# strands is a core dependency (see the module docstring): in the normal
# environment this import succeeds and STRANDS_AVAILABLE is True, which is the
# primary path. The guard only exists so a cold-start or stripped image can
# still fall back to the deterministic scorer instead of failing at import.
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

    The identity fallback means the decorated adapters below stay ordinary,
    directly-callable functions in an environment without ``strands`` — so the
    module imports cleanly and the adapters remain unit-testable. When
    ``strands`` *is* installed the real decorator runs and the agent gets proper
    tool specs.
    """
    if _strands_tool is not None:
        return _strands_tool(func)
    return func


# ---------------------------------------------------------------------------
# System prompt (design.md section 7 + NFR safety)
# ---------------------------------------------------------------------------

#: The structured-analysis prompt from design.md section 7. The wording follows
#: the NFR safety rule: never assert a repository is "dead", and never overreach
#: on a ``complex`` issue.
ANALYST_SYSTEM_PROMPT = """\
You are the Analyst in an autonomous open-source contribution pipeline.

Your job is to read a repository that appears to have reduced maintenance
activity, choose the single most valuable open issue, and judge honestly whether
an automated fix is realistic. You have read-only access. You never modify a
repository, never open pull requests, and never post comments.

Use your tools to gather context:
  - get_repo_issues        open issues, already ranked by thumbs-up reactions
  - get_repo_structure     the folder/file tree (partial for large repos)
  - get_file_contents      a specific file's text
  - score_issue_complexity a deterministic scorer you may consult as a sanity check

Analyse the top-voted candidate issue by looking for four concrete things:
  1. Reproduction steps  - can the failure be triggered deterministically?
  2. Error messages or stack traces - is the failure mode explicit?
  3. Referenced files - does the issue name the code responsible?
  4. Line numbers - is the location pinned down?

The more of these are present, the more tractable the issue. Absence of all four
means you cannot plan a safe change, however small the issue sounds.

Grade complexity as exactly one of:
  trivial   a localised change in one or two files, with an obvious test
  moderate  a contained change needing some investigation, still bounded
  complex   architectural, cross-cutting, concurrency-related, ambiguous, or
            under-specified work

Respond with ONLY this JSON object and no other text:

{
  "complexity": "trivial | moderate | complex",
  "confidence": 0.0-1.0,
  "files_affected": ["path/one.py"],
  "approach": "short natural-language plan"
}

Rules for your answer:
  - "confidence" is your confidence in this assessment, not your optimism about
    the fix. Report low confidence when the evidence is thin; that is a useful
    answer, not a failure.
  - "files_affected" must be real paths you saw in the repository tree or in the
    issue text. Do not invent paths.
  - "approach" must be a concrete plan, not a restatement of the issue.
  - Prefer grading "complex" over guessing. Declining an issue is cheap;
    a low-quality pull request costs a maintainer their time.
  - Describe the repository as appearing to have reduced maintenance activity.
    Never describe a project or its maintainers as dead or abandoned.
"""


# ---------------------------------------------------------------------------
# Tool adapters (design.md section 3 lists exactly these four)
# ---------------------------------------------------------------------------


@tool
def get_repo_issues(repo_full_name: str, limit: int = 10) -> str:
    """List a repository's open issues, most thumbs-up-reacted first.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        limit: Maximum number of issues to return.

    Returns:
        A JSON array of issue objects with number, title, body, thumbs_up,
        total_reactions, comments, labels, and timestamps.
    """
    issues = github_tools.get_repo_issues(repo_full_name, limit=limit)
    return json.dumps([issue.to_dict() for issue in issues])


@tool
def get_file_contents(repo_full_name: str, path: str, ref: str = "") -> str:
    """Read one file from a repository as text.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        path: Path to the file within the repository.
        ref: Optional branch, tag, or commit SHA. Empty means the default branch.

    Returns:
        The file's text, or a short message when the path is missing, is a
        directory, or is not readable as text.
    """
    text = github_tools.get_file_contents(
        repo_full_name, path, ref=ref or None
    )
    if text is None:
        return f"[no readable text at {path!r} in {repo_full_name}]"
    return text


@tool
def get_repo_structure(repo_full_name: str, ref: str = "") -> str:
    """List a repository's files and folders, capped in depth and count.

    Args:
        repo_full_name: Repository in ``owner/repo`` form.
        ref: Optional branch, tag, or commit SHA. Empty means the default branch.

    Returns:
        A JSON object with the tree entries and a ``truncated`` flag that is true
        when the listing is partial.
    """
    structure = github_tools.get_repo_structure(repo_full_name, ref=ref or None)
    return json.dumps(structure.to_dict())


@tool
def score_issue_complexity(
    title: str, body: str = "", labels: str = ""
) -> str:
    """Score one issue's complexity with the deterministic heuristic scorer.

    Use this as a sanity check on your own judgement. It inspects the text for
    reproduction steps, error messages, referenced files, and line numbers.

    Args:
        title: The issue title.
        body: The issue body text.
        labels: Comma-separated label names, if any.

    Returns:
        A JSON object with complexity, confidence, files_affected, and approach.
    """
    from src.tools.complexity_scorer import score_issue_complexity as _score

    label_list = [part.strip() for part in (labels or "").split(",") if part.strip()]
    return _score(title, body or None, labels=label_list).to_json()


#: The tool set from design.md section 3, in the order it is documented there.
ANALYST_TOOLS = (
    get_repo_issues,
    get_file_contents,
    get_repo_structure,
    score_issue_complexity,
)


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class AnalystReport:
    """What the Analyst hands back to the Orchestrator.

    ``result`` is the REQ-2.2 verdict and ``gate`` is the REQ-2.3 decision. The
    Analyst does not act on the gate; the Orchestrator does.

    ``source`` records how the verdict was produced, which matters for
    debugging and for the ``notes`` the Orchestrator persists:

    - ``"model"``     the model returned parsable JSON
    - ``"heuristic"`` the deterministic scorer was used (no model, or the model
                      output could not be parsed)
    - ``"none"``      nothing could be scored, e.g. the repo has no open issues
    """

    repo_full_name: str
    issue_number: Optional[int]
    issue_title: Optional[str]
    issue_url: Optional[str]
    result: Optional[ComplexityResult]
    source: str
    gate: GateDecision
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "issue_url": self.issue_url,
            "result": self.result.to_dict() if self.result else None,
            "source": self.source,
            "gate": self.gate.to_dict(),
            "notes": self.notes,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string — the agent-as-tool return shape."""
        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

#: Characters of README included in the analysis prompt.
README_PROMPT_CHARS = 1500
#: Characters of issue body included in the analysis prompt.
ISSUE_BODY_PROMPT_CHARS = 6000
#: Tree paths included in the analysis prompt.
STRUCTURE_PROMPT_ENTRIES = 120
#: Commit subject lines included in the analysis prompt.
COMMITS_PROMPT_ENTRIES = 10


def _clip(text: Optional[str], limit: int) -> str:
    """Clip ``text`` to ``limit`` characters with an explicit marker."""
    if not text:
        return "(none)"
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[clipped at {limit} characters]"


def build_analysis_prompt(context: RepoContext, issue: IssueSummary) -> str:
    """Render the per-repo analysis task for the model.

    Packs everything REQ-2.1 requires into one message so the model can answer
    without spending tool calls re-reading what we already fetched. Every
    section is length-capped (see the ``*_PROMPT_*`` constants) to keep the
    prompt inside a predictable budget.
    """
    structure_paths = (
        context.structure.paths()[:STRUCTURE_PROMPT_ENTRIES]
        if context.structure
        else []
    )
    commit_lines = [
        f"- {commit.sha[:7]} {commit.message.splitlines()[0] if commit.message else ''}"
        for commit in context.recent_commits[:COMMITS_PROMPT_ENTRIES]
    ]
    other_issues = [
        f"- #{i.number} ({i.thumbs_up} thumbs-up) {i.title}"
        for i in context.issues[1:6]
    ]

    return f"""\
Repository: {context.repo_full_name}
Primary language: {context.primary_language or "unknown"}
Default branch: {context.default_branch or "unknown"}

This repository appears to have reduced maintenance activity.

README (clipped):
{_clip(context.readme, README_PROMPT_CHARS)}

Repository structure ({len(structure_paths)} entries shown\
{", partial" if context.structure and context.structure.truncated else ""}):
{chr(10).join("- " + p for p in structure_paths) or "(unavailable)"}

Last {len(commit_lines)} commits:
{chr(10).join(commit_lines) or "(unavailable)"}

Top-voted candidate issue
-------------------------
Number: #{issue.number}
Title: {issue.title}
Thumbs-up reactions: {issue.thumbs_up} (total reactions: {issue.total_reactions})
Comments: {issue.comments}
Labels: {", ".join(issue.labels) or "(none)"}

Body:
{_clip(issue.body, ISSUE_BODY_PROMPT_CHARS)}

Other open issues by reaction count:
{chr(10).join(other_issues) or "(none)"}

Score this issue now. Reply with only the JSON object.
"""


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def build_analyst(
    *,
    model: Any = None,
    tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """Construct the Analyst ``strands.Agent``.

    ``model`` is passed through to ``strands.Agent`` so the Orchestrator (or a
    test) can pin a specific Bedrock model; ``None`` lets strands apply its
    configured default.

    Raises:
        StrandsUnavailableError: when ``strands`` is not installed. Callers that
            need to keep working regardless should use :func:`analyze_repo`,
            which falls back to the deterministic scorer.
    """
    if not STRANDS_AVAILABLE or _StrandsAgent is None:
        raise StrandsUnavailableError(
            "strands-agents is not installed; the model-backed Analyst is "
            "unavailable. analyze_repo() still works via the deterministic "
            "complexity_scorer fallback."
        )
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt or ANALYST_SYSTEM_PROMPT,
        "tools": list(tools) if tools is not None else list(ANALYST_TOOLS),
    }
    if model is not None:
        kwargs["model"] = model
    return _StrandsAgent(**kwargs)


def _model_runner(agent: Any) -> Callable[[str], str]:
    """Adapt a strands ``Agent`` to the ``Callable[[str], str]`` shape.

    Invocation convention, verified by inspecting the installed
    **strands-agents 1.55.1** package (not from memory):

    - ``Agent.__call__(self, prompt, ...) -> AgentResult`` — calling the agent
      with a string prompt runs the event loop and returns an
      ``strands.agent.agent_result.AgentResult``.
    - ``AgentResult.__str__`` yields the model's text: its documented priority
      order is interrupts, then structured output (as ``model_dump_json()``),
      then the concatenated ``text`` blocks of ``self.message["content"]``.

    So ``str(agent(prompt))`` is the correct way to obtain the model's text
    output in this SDK version — the result's ``__str__`` already does the
    text-block extraction for us. We call the agent and stringify the result,
    which downstream is handed to
    :func:`~src.tools.complexity_scorer.parse_model_output`.

    The real Bedrock round-trip behind ``agent(prompt)`` is the single piece
    that remains unverified without AWS credentials / model access (Task 13);
    every offline path injects ``run_model`` or a fake ``agent`` instead.
    """

    def run(prompt: str) -> str:
        return str(agent(prompt))

    return run


# ---------------------------------------------------------------------------
# Analysis (REQ-2.1, REQ-2.2, REQ-2.3)
# ---------------------------------------------------------------------------


def analyze_repo(
    repo_full_name: str,
    *,
    client: Any = None,
    token: Optional[str] = None,
    context: Optional[RepoContext] = None,
    run_model: Optional[Callable[[str], str]] = None,
    agent: Any = None,
    use_model: bool = True,
    threshold: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AnalystReport:
    """Analyse one repository and score its top-voted issue.

    Steps:

    1. Gather everything REQ-2.1 requires (or accept a pre-built ``context``).
    2. Take the top-voted open issue (REQ-2.2). No open issues means no verdict.
    3. Ask the model for the structured JSON; if the output is unparsable, the
       model raises, or no model is available, fall back to the deterministic
       :func:`~src.tools.complexity_scorer.score_issue_complexity` (design.md
       sections 7 and 10).
    4. Apply the REQ-2.3 gate and return it — without persisting anything.

    Injection points, all of which keep this function runnable offline:
    ``context`` skips the GitHub reads, ``run_model`` supplies raw model text,
    ``agent`` supplies a pre-built agent, and ``use_model=False`` forces the
    deterministic path.
    """
    if context is None:
        context = github_tools.gather_repo_context(
            repo_full_name, client=client, token=token, sleep=sleep
        )

    issue = context.top_issue()
    if issue is None:
        gate = gate_decision(None, threshold=threshold)
        return AnalystReport(
            repo_full_name=repo_full_name,
            issue_number=None,
            issue_title=None,
            issue_url=None,
            result=None,
            source="none",
            gate=gate,
            notes="no open issues to analyse",
        )

    runner = run_model
    if runner is None and agent is not None:
        runner = _model_runner(agent)
    if runner is None and use_model and STRANDS_AVAILABLE:
        try:
            # Lazy import keeps the model-provider seam off the injected paths.
            from src.tools.model_provider import get_default_model

            runner = _model_runner(build_analyst(model=get_default_model()))
        except Exception:  # noqa: BLE001 - degrade to the heuristic, never crash
            LOGGER.warning("could not build the Analyst agent", exc_info=True)
            runner = None

    result: Optional[ComplexityResult] = None
    source = "heuristic"
    notes = ""

    if runner is not None:
        prompt = build_analysis_prompt(context, issue)
        try:
            raw = runner(prompt)
        except Exception as exc:  # noqa: BLE001 - Bedrock throttling, transport, etc.
            LOGGER.warning("Analyst model call failed: %s", exc)
            notes = f"model call failed ({exc.__class__.__name__}); used heuristic scorer"
        else:
            result = parse_model_output(raw)
            if result is not None:
                source = "model"
            else:
                notes = "model output was not parsable JSON; used heuristic scorer"
    else:
        notes = (
            "no model backend available; used heuristic scorer"
            if use_model
            else "heuristic scorer requested"
        )

    if result is None:
        result = score_issue(issue)

    gate = gate_decision(result, threshold=threshold)
    summary = (
        f"issue #{issue.number} scored {result.complexity} "
        f"(confidence {result.confidence}, via {source})"
    )
    return AnalystReport(
        repo_full_name=repo_full_name,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_url=issue.html_url,
        result=result,
        source=source,
        gate=gate,
        notes=f"{summary}; {notes}" if notes else summary,
    )


# ---------------------------------------------------------------------------
# Agent-as-tool entry point (design.md section 8)
# ---------------------------------------------------------------------------


@tool
def analyst_agent(repo_full_name: str) -> str:
    """Analyze a repository and score its top-voted open issue. Returns JSON.

    This is the Orchestrator's handle on the Analyst (design.md section 8).

    Args:
        repo_full_name: Repository in ``owner/repo`` form.

    Returns:
        A JSON object with the scored issue, the ``{complexity, confidence,
        files_affected, approach}`` verdict, and the gating decision telling the
        Orchestrator whether to continue or record ``skipped_complex``.
    """
    return analyze_repo(repo_full_name).to_json()
