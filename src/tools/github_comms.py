"""GitHub **comms** operations backing the Communicator sub-agent.

The Communicator is the only agent that opens pull requests or posts comments
(design.md section 3). This module holds the plain, framework-free functions
behind its GitHub-mutating tools:

- :func:`open_pr` — open the PR for a fix branch (REQ-4.1, REQ-4.2)
- :func:`post_issue_comment` — comment on the original issue, linking the PR (REQ-4.3)
- :func:`post_pr_comment` — reply on the PR conversation (REQ-5.3)

Why a third GitHub module (alongside ``github_tools`` and ``github_write``)
---------------------------------------------------------------------------
The codebase already keeps GitHub **reads** (:mod:`src.tools.github_tools`, the
Analyst's surface) and code **writes** (:mod:`src.tools.github_write`, the
Engineer's surface) in separate modules so the read-only / write boundary can be
audited by imports rather than by reading every function. The Communicator is a
third, distinct kind of write: it mutates the *conversation* (PRs and comments),
never the *code* (branches, files, commits). Folding PR/comment creation into
``github_write`` would have put "opens a PR" one autocomplete away from "pushes a
commit" and blurred the design.md section-3 boundary that says the Engineer does
one and the Communicator does the other. Three modules, one kind of mutation
each: none, code, conversation.

All three share :func:`src.tools.github_tools.resolve_client` (the single token
resolver: injected client → token → ``GITHUB_TOKEN`` → the Secrets-Manager
loader cached per container) and :func:`src.tools.github_search.with_backoff`
(the single rate-limit backoff — design.md section 10). Neither is re-implemented
here.

The wording lives elsewhere
----------------------------
Every string a maintainer reads is built by :mod:`src.tools.pr_templates` (pure,
dependency-free, exhaustively testable). This module is only the PyGithub plumbing
that carries those strings to GitHub and returns a structured result.

The DynamoDB write is not here (REQ-4.4)
----------------------------------------
REQ-4.4 records ``pr_opened`` with ``pr_number`` / ``pr_url`` / ``issue_number`` /
``opened_at``, but the Orchestrator is the only component that writes state
(design.md section 3). So :func:`open_pr` **returns** those fields on
:class:`PROpenResult` for the Orchestrator (Task 8) to persist. Nothing here
imports the state layer.

No ``strands`` import lives here. The ``@tool`` adapters are in
:mod:`src.agents.communicator`; everything below is an ordinary function so it
stays importable and testable without the agent framework or a network.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from github import Github
from github.GithubException import GithubException

from src.tools import pr_templates
from src.tools.github_search import with_backoff
from src.tools.github_tools import resolve_client

LOGGER = logging.getLogger(__name__)

#: HTTP 422 is what GitHub returns from ``POST /pulls`` when a PR for that head
#: branch already exists — see :func:`open_pr` for why that is idempotent success.
_UNPROCESSABLE = 422


def _utcnow_iso(now: Optional[Callable[[], datetime]] = None) -> str:
    """Return an ISO-8601 UTC timestamp (``opened_at``, REQ-4.4).

    ``now`` is injectable so tests get a deterministic timestamp.
    """
    stamp = (now() if now is not None else datetime.now(timezone.utc))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Result models — JSON-serializable, fed back to the Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class PROpenResult:
    """The outcome of opening a PR (REQ-4.1, REQ-4.2, REQ-4.4).

    Carries exactly the fields REQ-4.4 asks the Orchestrator to persist —
    ``pr_number`` / ``pr_url`` / ``issue_number`` / ``opened_at`` — plus the
    ``branch`` and a ``created`` flag (``False`` when an existing PR was reused,
    see :func:`open_pr`). ``success`` / ``reason`` let a failure come back as a
    value the Orchestrator can reason over instead of an exception (design.md
    section 10).
    """

    repo_full_name: str
    success: bool
    reason: str = ""
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    issue_number: Optional[int] = None
    opened_at: Optional[str] = None
    branch: Optional[str] = None
    base_branch: Optional[str] = None
    title: Optional[str] = None
    created: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "success": self.success,
            "reason": self.reason,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "issue_number": self.issue_number,
            "opened_at": self.opened_at,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "title": self.title,
            "created": self.created,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string — the agent-as-tool return shape."""
        import json

        return json.dumps(self.to_dict(), **kwargs)


@dataclass
class CommentResult:
    """The outcome of posting one comment (REQ-4.3, REQ-5.3).

    ``target`` is ``"issue"`` or ``"pr"`` so a trace makes clear which
    conversation the comment landed on.
    """

    repo_full_name: str
    success: bool
    target: str
    target_number: Optional[int] = None
    comment_id: Optional[int] = None
    comment_url: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "success": self.success,
            "target": self.target,
            "target_number": self.target_number,
            "comment_id": self.comment_id,
            "comment_url": self.comment_url,
            "reason": self.reason,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string — the agent-as-tool return shape."""
        import json

        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# Repo resolution
# ---------------------------------------------------------------------------


def _resolve_repo(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Return an already-fetched ``repo`` or look one up, with backoff.

    Mirrors the helper in the read and write modules so the Communicator can be
    handed a repo it already has, or resolve one from a client/token.
    """
    if repo is not None:
        return repo
    gh = resolve_client(client, token)
    return with_backoff(lambda: gh.get_repo(repo_full_name), sleep=sleep)


def _find_open_pr_for_head(
    repo: Any, head_branch: str, *, sleep: Callable[[float], None]
) -> Any:
    """Return the open PR whose head is ``head_branch``, or ``None``.

    Used for the already-exists recovery in :func:`open_pr`. Walks the open PRs
    and matches on the head ref name, which is enough because the fix branch name
    is unique per issue (``resurrector/fix-issue-{N}``).
    """
    pulls = with_backoff(lambda: repo.get_pulls(state="open"), sleep=sleep)
    for pull in pulls:
        head = getattr(pull, "head", None)
        ref = getattr(head, "ref", None) if head is not None else None
        if ref == head_branch:
            return pull
    return None


# ---------------------------------------------------------------------------
# open_pr (REQ-4.1, REQ-4.2)
# ---------------------------------------------------------------------------


def open_pr(
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
    client: Optional[Github] = None,
    token: Optional[str] = None,
    now: Optional[Callable[[], datetime]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PROpenResult:
    """Open the PR for a fix branch (REQ-4.1, REQ-4.2).

    The title is :func:`src.tools.pr_templates.format_pr_title` (exactly
    ``[Resurrector] Fix: {issue title} (closes #{N})``) and the body is
    :func:`src.tools.pr_templates.format_pr_body` (the four fixed sections with
    ``closes #{N}``). The base defaults to the repository's ``default_branch``.

    **Idempotency.** GitHub answers ``POST /pulls`` with HTTP 422 when a PR for
    that head branch already exists. Because the Processor's SQS FIFO message can
    be redelivered (visibility-timeout expiry, a Lambda retry) and the fix branch
    name is a pure function of the issue number, a second run must be able to
    continue rather than dead-end. So a 422 is treated as **success**: we look up
    the existing open PR for that head and return it with ``created=False``. This
    mirrors the "reuse the branch" choice :func:`src.tools.github_write.create_branch`
    already makes for the same reason.

    Returns a :class:`PROpenResult`. A failure comes back as ``success=False``
    with a ``reason`` rather than raising, so the Orchestrator can reason over it
    (design.md section 10) — except for a truly unexpected transport error, which
    still propagates.
    """
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    base = base_branch or getattr(target, "default_branch", None)
    if not base:
        return PROpenResult(
            repo_full_name=repo_full_name,
            success=False,
            reason=(
                f"could not determine the default branch of {repo_full_name}; "
                "pass base_branch explicitly"
            ),
            issue_number=issue_number,
            branch=head_branch,
        )

    title = pr_templates.format_pr_title(issue_title, issue_number)
    body = pr_templates.format_pr_body(
        issue_number=issue_number,
        what_changed=what_changed,
        why=why,
        how_to_test=how_to_test,
    )

    def _build_result(pull: Any, *, created: bool) -> PROpenResult:
        return PROpenResult(
            repo_full_name=repo_full_name,
            success=True,
            reason="opened" if created else "reused existing PR for this branch",
            pr_number=int(getattr(pull, "number")),
            pr_url=getattr(pull, "html_url", None),
            issue_number=int(issue_number),
            opened_at=_utcnow_iso(now),
            branch=head_branch,
            base_branch=base,
            title=title,
            created=created,
        )

    try:
        pull = with_backoff(
            lambda: target.create_pull(
                title=title, body=body, head=head_branch, base=base
            ),
            sleep=sleep,
        )
    except GithubException as exc:
        if exc.status != _UNPROCESSABLE:
            raise
        LOGGER.info(
            "a PR for %s already exists in %s; reusing it",
            head_branch,
            repo_full_name,
        )
        existing = _find_open_pr_for_head(target, head_branch, sleep=sleep)
        if existing is None:
            return PROpenResult(
                repo_full_name=repo_full_name,
                success=False,
                reason=(
                    "GitHub reported a PR already exists for "
                    f"{head_branch} but it could not be found"
                ),
                issue_number=int(issue_number),
                branch=head_branch,
                base_branch=base,
                title=title,
            )
        return _build_result(existing, created=False)

    return _build_result(pull, created=True)


# ---------------------------------------------------------------------------
# post_issue_comment (REQ-4.3)
# ---------------------------------------------------------------------------


def post_issue_comment(
    repo_full_name: str,
    issue_number: int,
    body: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CommentResult:
    """Post a comment on the original issue (REQ-4.3).

    ``repo.get_issue(issue_number).create_comment(body)`` — the standard issue
    comment endpoint. Returns a :class:`CommentResult` with the comment's URL and
    id for the trace.
    """
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    issue = with_backoff(lambda: target.get_issue(int(issue_number)), sleep=sleep)
    comment = with_backoff(lambda: issue.create_comment(body), sleep=sleep)
    return CommentResult(
        repo_full_name=repo_full_name,
        success=True,
        target="issue",
        target_number=int(issue_number),
        comment_id=getattr(comment, "id", None),
        comment_url=getattr(comment, "html_url", None),
        reason="posted",
    )


# ---------------------------------------------------------------------------
# post_pr_comment (REQ-5.3)
# ---------------------------------------------------------------------------


def post_pr_comment(
    repo_full_name: str,
    pr_number: int,
    body: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CommentResult:
    """Post a comment on the PR conversation (REQ-5.3).

    A pull request *is* an issue in GitHub's data model, and the two ways to add
    a conversation (non-review) comment are ``repo.get_issue(pr_number)
    .create_comment(...)`` and ``repo.get_pull(pr_number).create_issue_comment(...)``.
    Both hit ``POST /issues/{n}/comments`` and land in the same place. We use
    ``get_issue`` deliberately: it is the lighter call — ``get_pull`` fetches the
    full pull-request object (diffstat, mergeability, head/base refs) we do not
    need just to post a comment — and it keeps this symmetric with
    :func:`post_issue_comment`. This posts to the conversation timeline, **not**
    an inline diff/review comment, which is the right surface for answering a
    maintainer's question (REQ-5.3).
    """
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    issue = with_backoff(lambda: target.get_issue(int(pr_number)), sleep=sleep)
    comment = with_backoff(lambda: issue.create_comment(body), sleep=sleep)
    return CommentResult(
        repo_full_name=repo_full_name,
        success=True,
        target="pr",
        target_number=int(pr_number),
        comment_id=getattr(comment, "id", None),
        comment_url=getattr(comment, "html_url", None),
        reason="posted",
    )
