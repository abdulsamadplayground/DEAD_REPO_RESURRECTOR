"""Unit tests for src.tools.github_comms (Communicator write ops).

Fully offline: fake PyGithub objects stand in for Repository / PullRequest /
Issue / IssueComment. No network, no AWS, no strands.

Covers REQ-4.1/4.2 (open_pr uses the exact title/body and returns the REQ-4.4
fields), the already-exists (HTTP 422) idempotency path, REQ-4.3
(post_issue_comment), REQ-5.3 (post_pr_comment via get_issue), and rate-limit
backoff (design.md section 10).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from github.GithubException import GithubException, RateLimitExceededException

from src.tools import github_comms as gc


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeComment:
    def __init__(self, comment_id=1, html_url="https://github.com/o/r/issues/1#c1"):
        self.id = comment_id
        self.html_url = html_url


class FakeIssue:
    def __init__(self, number):
        self.number = number
        self.comments_posted = []

    def create_comment(self, body):
        self.comments_posted.append(body)
        return FakeComment(
            comment_id=100 + self.number,
            html_url=f"https://github.com/o/r/issues/{self.number}#c{len(self.comments_posted)}",
        )


class FakeHead:
    def __init__(self, ref):
        self.ref = ref


class FakePull:
    def __init__(self, number, head_ref, html_url=None):
        self.number = number
        self.head = FakeHead(head_ref)
        self.html_url = html_url or f"https://github.com/o/r/pull/{number}"


class FakeRepo:
    """The subset of a PyGithub Repository that github_comms touches."""

    def __init__(
        self,
        *,
        default_branch="main",
        create_pull_error=None,
        existing_pulls=(),
    ):
        self.default_branch = default_branch
        self.full_name = "owner/repo"
        self._create_pull_error = create_pull_error
        self._existing_pulls = list(existing_pulls)
        self.created_pulls = []
        self.issues_fetched = []

    def create_pull(self, title, body, head, base):
        if self._create_pull_error is not None:
            error, self._create_pull_error = self._create_pull_error, None
            raise error
        pull = FakePull(number=321, head_ref=head)
        self.created_pulls.append(
            {"title": title, "body": body, "head": head, "base": base}
        )
        return pull

    def get_pulls(self, state=None):
        return list(self._existing_pulls)

    def get_issue(self, number):
        issue = FakeIssue(number)
        self.issues_fetched.append(number)
        return issue


FIXED_NOW = lambda: datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# open_pr — REQ-4.1, REQ-4.2, REQ-4.4
# ---------------------------------------------------------------------------


def test_open_pr_creates_with_exact_title_and_body():
    repo = FakeRepo()
    result = gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-42",
        issue_number=42,
        issue_title="Crash on empty config",
        what_changed="Guard the empty case.",
        why="Empty configs crashed the loader.",
        how_to_test="Run pytest.",
        repo=repo,
        now=FIXED_NOW,
    )
    assert result.success is True
    assert result.created is True
    call = repo.created_pulls[0]
    assert call["title"] == "[Resurrector] Fix: Crash on empty config (closes #42)"
    assert call["head"] == "resurrector/fix-issue-42"
    assert call["base"] == "main"
    assert "## What changed" in call["body"]
    assert "closes #42" in call["body"]


def test_open_pr_returns_req_4_4_fields():
    repo = FakeRepo()
    result = gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-7",
        issue_number=7,
        issue_title="Bug",
        what_changed="Fix.",
        repo=repo,
        now=FIXED_NOW,
    )
    assert result.pr_number == 321
    assert result.pr_url == "https://github.com/o/r/pull/321"
    assert result.issue_number == 7
    assert result.opened_at == "2024-01-02T03:04:05+00:00"
    assert result.branch == "resurrector/fix-issue-7"


def test_open_pr_uses_explicit_base_branch():
    repo = FakeRepo(default_branch="main")
    gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-1",
        issue_number=1,
        issue_title="t",
        what_changed="c",
        base_branch="develop",
        repo=repo,
    )
    assert repo.created_pulls[0]["base"] == "develop"


def test_open_pr_already_exists_returns_existing_without_raising():
    existing = FakePull(number=999, head_ref="resurrector/fix-issue-42")
    repo = FakeRepo(
        create_pull_error=GithubException(422, {"message": "A pull request already exists"}, {}),
        existing_pulls=[FakePull(1, "other-branch"), existing],
    )
    result = gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-42",
        issue_number=42,
        issue_title="Crash on empty config",
        what_changed="Guard.",
        repo=repo,
        now=FIXED_NOW,
    )
    assert result.success is True
    assert result.created is False
    assert result.pr_number == 999
    assert result.pr_url == "https://github.com/o/r/pull/999"


def test_open_pr_missing_default_branch_fails_softly():
    repo = FakeRepo(default_branch=None)
    result = gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-1",
        issue_number=1,
        issue_title="t",
        what_changed="c",
        repo=repo,
    )
    assert result.success is False
    assert "default branch" in result.reason


def test_open_pr_non_422_error_propagates():
    repo = FakeRepo(create_pull_error=GithubException(500, {"message": "boom"}, {}))
    with pytest.raises(GithubException):
        gc.open_pr(
            "owner/repo",
            head_branch="resurrector/fix-issue-1",
            issue_number=1,
            issue_title="t",
            what_changed="c",
            repo=repo,
        )


# ---------------------------------------------------------------------------
# post_issue_comment — REQ-4.3
# ---------------------------------------------------------------------------


def test_post_issue_comment_posts_and_returns_url():
    repo = FakeRepo()
    result = gc.post_issue_comment("owner/repo", 42, "Opened a PR: ...", repo=repo)
    assert result.success is True
    assert result.target == "issue"
    assert result.target_number == 42
    assert result.comment_url is not None
    assert 42 in repo.issues_fetched


# ---------------------------------------------------------------------------
# post_pr_comment — REQ-5.3 (get_issue choice)
# ---------------------------------------------------------------------------


def test_post_pr_comment_uses_get_issue_and_returns_url():
    repo = FakeRepo()
    result = gc.post_pr_comment("owner/repo", 321, "Answering your question ...", repo=repo)
    assert result.success is True
    assert result.target == "pr"
    assert result.target_number == 321
    assert result.comment_url is not None
    # a PR is an issue: we resolved it through get_issue(pr_number)
    assert 321 in repo.issues_fetched


# ---------------------------------------------------------------------------
# Rate-limit backoff (design.md section 10)
# ---------------------------------------------------------------------------


class FlakyRepo(FakeRepo):
    """create_pull raises a rate-limit error once, then succeeds."""

    def __init__(self):
        super().__init__()
        self._raised = False

    def create_pull(self, title, body, head, base):
        if not self._raised:
            self._raised = True
            raise RateLimitExceededException(403, {"message": "rate limited"}, {})
        return super().create_pull(title, body, head, base)


def test_open_pr_retries_on_rate_limit(monkeypatch):
    slept = []
    repo = FlakyRepo()
    result = gc.open_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-1",
        issue_number=1,
        issue_title="t",
        what_changed="c",
        repo=repo,
        now=FIXED_NOW,
        sleep=lambda s: slept.append(s),
    )
    assert result.success is True
    assert repo._raised is True
    assert slept, "backoff should have slept at least once before retrying"
