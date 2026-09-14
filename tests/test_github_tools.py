"""Unit tests for src.tools.github_tools (Analyst read tools).

Fully offline: fake PyGithub objects stand in for Repository / Issue /
ContentFile / GitTree / Commit, and a fake client stands in for
``github.Github``. No network, no AWS, no ``strands``.

Covers REQ-2.1: open issues sorted by 👍 reactions, README, primary language,
folder structure, and the last 10 commits — plus the context-budget caps and
rate-limit backoff (design.md section 10).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from github.GithubException import RateLimitExceededException, UnknownObjectException

import src.tools.github_tools as gt


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeLabel:
    def __init__(self, name):
        self.name = name


class FakeIssue:
    """Mimics the subset of a PyGithub Issue that github_tools reads."""

    def __init__(
        self,
        number,
        title="An issue",
        body="body text",
        thumbs_up=0,
        total_reactions=None,
        comments=0,
        labels=(),
        is_pr=False,
        reactions_dict=True,
    ):
        self.number = number
        self.title = title
        self.body = body
        self.comments = comments
        self.labels = [FakeLabel(name) for name in labels]
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.updated_at = datetime(2021, 1, 1, tzinfo=timezone.utc)
        self.html_url = f"https://github.com/o/r/issues/{number}"
        self.pull_request = object() if is_pr else None
        total = total_reactions if total_reactions is not None else thumbs_up
        if reactions_dict:
            self.reactions = {"+1": thumbs_up, "total_count": total}
        else:
            # Force the get_reactions() fallback path.
            self.reactions = None
            self._reaction_contents = ["+1"] * thumbs_up + ["heart"] * (
                max(total - thumbs_up, 0)
            )

    def get_reactions(self):
        return [type("R", (), {"content": c})() for c in self._reaction_contents]


class FakeContentFile:
    def __init__(self, content=b"hello", raises=False):
        self._content = content
        self._raises = raises

    @property
    def decoded_content(self):
        if self._raises:
            raise ValueError("cannot decode")
        return self._content


class FakeTreeElement:
    def __init__(self, path, type_="blob", size=10):
        self.path = path
        self.type = type_
        self.size = size


class FakeTree:
    def __init__(self, paths, truncated=False):
        self.tree = [
            p if isinstance(p, FakeTreeElement) else FakeTreeElement(p) for p in paths
        ]
        self.truncated = truncated


class FakeGitAuthor:
    def __init__(self, name, date):
        self.name = name
        self.date = date


class FakeGitCommit:
    def __init__(self, message, author):
        self.message = message
        self.author = author


class FakeCommit:
    def __init__(self, sha, message="a commit", author="Dev"):
        self.sha = sha
        self.commit = FakeGitCommit(
            message, FakeGitAuthor(author, datetime(2022, 5, 1, tzinfo=timezone.utc))
        )
        self.author = None
        self.html_url = f"https://github.com/o/r/commit/{sha}"


class FakeRepo:
    """Mimics the subset of a PyGithub Repository that github_tools reads."""

    def __init__(
        self,
        full_name="owner/repo",
        issues=(),
        contents=None,
        tree=None,
        readme=None,
        commits=(),
        language="Python",
        default_branch="main",
        readme_error=None,
        contents_error=None,
    ):
        self.full_name = full_name
        self.language = language
        self.default_branch = default_branch
        self._issues = list(issues)
        self._contents = contents
        self._tree = tree
        self._readme = readme
        self._commits = list(commits)
        self._readme_error = readme_error
        self._contents_error = contents_error
        self.tree_ref = None
        self.issues_state = None

    def get_issues(self, state=None):
        self.issues_state = state
        return list(self._issues)

    def get_contents(self, path, ref=None):
        if self._contents_error is not None:
            raise self._contents_error
        return self._contents

    def get_git_tree(self, ref, recursive=False):
        self.tree_ref = ref
        return self._tree

    def get_readme(self, ref=None):
        if self._readme_error is not None:
            raise self._readme_error
        return self._readme

    def get_commits(self):
        return list(self._commits)


class FakeClient:
    """Stand-in for github.Github; can fail N times before succeeding."""

    def __init__(self, repo, fail_times=0, exc=None):
        self._repo = repo
        self._fail_times = fail_times
        self._exc = exc
        self.call_count = 0

    def get_repo(self, full_name):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._exc
        return self._repo


def _no_sleep(_delay):
    """Backoff sleep stub so tests never actually wait."""
    return None


# ---------------------------------------------------------------------------
# get_repo_issues — reaction sorting (REQ-2.1)
# ---------------------------------------------------------------------------


def test_issues_sorted_by_thumbs_up_descending():
    repo = FakeRepo(
        issues=[
            FakeIssue(1, thumbs_up=2),
            FakeIssue(2, thumbs_up=9),
            FakeIssue(3, thumbs_up=5),
        ]
    )
    issues = gt.get_repo_issues("owner/repo", repo=repo, sleep=_no_sleep)
    assert [i.number for i in issues] == [2, 3, 1]
    assert [i.thumbs_up for i in issues] == [9, 5, 2]
    assert repo.issues_state == "open"


def test_thumbs_up_ties_break_on_total_reactions_then_comments_then_number():
    repo = FakeRepo(
        issues=[
            # All tied at 3 thumbs-up.
            FakeIssue(10, thumbs_up=3, total_reactions=3, comments=0),
            FakeIssue(11, thumbs_up=3, total_reactions=8, comments=0),  # most reactions
            FakeIssue(12, thumbs_up=3, total_reactions=3, comments=7),  # most comments
            FakeIssue(9, thumbs_up=3, total_reactions=3, comments=0),  # lowest number
        ]
    )
    issues = gt.get_repo_issues("owner/repo", repo=repo, sleep=_no_sleep)
    # total reactions -> comments -> issue number ascending
    assert [i.number for i in issues] == [11, 12, 9, 10]


def test_issue_ordering_is_deterministic_regardless_of_input_order():
    forward = [FakeIssue(n, thumbs_up=0) for n in (1, 2, 3, 4)]
    backward = [FakeIssue(n, thumbs_up=0) for n in (4, 3, 2, 1)]
    a = gt.get_repo_issues("o/r", repo=FakeRepo(issues=forward), sleep=_no_sleep)
    b = gt.get_repo_issues("o/r", repo=FakeRepo(issues=backward), sleep=_no_sleep)
    assert [i.number for i in a] == [i.number for i in b] == [1, 2, 3, 4]


def test_pull_requests_are_excluded_from_issues():
    repo = FakeRepo(
        issues=[
            FakeIssue(1, thumbs_up=1),
            FakeIssue(2, thumbs_up=99, is_pr=True),
        ]
    )
    issues = gt.get_repo_issues("owner/repo", repo=repo, sleep=_no_sleep)
    assert [i.number for i in issues] == [1]


def test_issue_limit_and_scan_limit_are_respected():
    repo = FakeRepo(issues=[FakeIssue(n, thumbs_up=n) for n in range(1, 21)])
    assert len(gt.get_repo_issues("o/r", repo=repo, limit=3, sleep=_no_sleep)) == 3
    # Only the first 5 issues are inspected, so the highest-numbered (and most
    # reacted) ones are never seen.
    scanned = gt.get_repo_issues("o/r", repo=repo, scan_limit=5, sleep=_no_sleep)
    assert [i.number for i in scanned] == [5, 4, 3, 2, 1]


def test_issue_limit_reads_env(monkeypatch):
    monkeypatch.setenv(gt.ISSUE_LIMIT_ENV_VAR, "2")
    repo = FakeRepo(issues=[FakeIssue(n, thumbs_up=n) for n in range(1, 6)])
    assert len(gt.get_repo_issues("o/r", repo=repo, sleep=_no_sleep)) == 2


def test_reaction_counts_fall_back_to_get_reactions():
    repo = FakeRepo(
        issues=[
            FakeIssue(1, thumbs_up=4, total_reactions=6, reactions_dict=False),
        ]
    )
    issues = gt.get_repo_issues("o/r", repo=repo, sleep=_no_sleep)
    assert issues[0].thumbs_up == 4
    assert issues[0].total_reactions == 6


def test_issue_summary_maps_labels_and_timestamps():
    repo = FakeRepo(issues=[FakeIssue(7, labels=("bug", "help wanted"), comments=3)])
    issue = gt.get_repo_issues("o/r", repo=repo, sleep=_no_sleep)[0]
    assert issue.labels == ["bug", "help wanted"]
    assert issue.comments == 3
    assert issue.created_at == "2020-01-01T00:00:00+00:00"
    assert issue.html_url.endswith("/issues/7")


# ---------------------------------------------------------------------------
# get_file_contents
# ---------------------------------------------------------------------------


def test_get_file_contents_returns_text():
    repo = FakeRepo(contents=FakeContentFile(b"print('hi')\n"))
    assert gt.get_file_contents("o/r", "a.py", repo=repo, sleep=_no_sleep) == "print('hi')\n"


def test_get_file_contents_missing_file_returns_none():
    repo = FakeRepo(contents_error=UnknownObjectException(404, {"message": "Not Found"}, {}))
    assert gt.get_file_contents("o/r", "nope.py", repo=repo, sleep=_no_sleep) is None


def test_get_file_contents_directory_returns_none():
    repo = FakeRepo(contents=[FakeContentFile(b"a"), FakeContentFile(b"b")])
    assert gt.get_file_contents("o/r", "src", repo=repo, sleep=_no_sleep) is None


def test_get_file_contents_binary_returns_none():
    repo = FakeRepo(contents=FakeContentFile(b"\xff\xfe\x00\x01\x02\x03"))
    assert gt.get_file_contents("o/r", "logo.png", repo=repo, sleep=_no_sleep) is None


def test_get_file_contents_undecodable_object_returns_none():
    repo = FakeRepo(contents=FakeContentFile(raises=True))
    assert gt.get_file_contents("o/r", "a.py", repo=repo, sleep=_no_sleep) is None


def test_get_file_contents_oversized_is_truncated_with_marker():
    repo = FakeRepo(contents=FakeContentFile(b"x" * 500))
    text = gt.get_file_contents("o/r", "big.py", repo=repo, max_bytes=20, sleep=_no_sleep)
    marker = gt.TRUNCATION_MARKER.format(limit=20)
    assert text.endswith(marker)
    # 20 bytes of content, not 500.
    assert text[: -len(marker)] == "x" * 20


def test_max_file_bytes_reads_env(monkeypatch):
    monkeypatch.setenv(gt.MAX_FILE_BYTES_ENV_VAR, "5")
    repo = FakeRepo(contents=FakeContentFile(b"abcdefghij"))
    text = gt.get_file_contents("o/r", "a.py", repo=repo, sleep=_no_sleep)
    assert text.startswith("abcde")
    assert "truncated" in text


# ---------------------------------------------------------------------------
# get_repo_structure
# ---------------------------------------------------------------------------


def test_repo_structure_respects_depth_cap():
    repo = FakeRepo(
        tree=FakeTree(["README.md", "src/a.py", "src/deep/b.py", "src/deep/er/c.py"])
    )
    structure = gt.get_repo_structure("o/r", repo=repo, max_depth=2, sleep=_no_sleep)
    assert structure.paths() == ["README.md", "src/a.py"]
    assert structure.truncated is True
    assert structure.max_depth == 2


def test_repo_structure_respects_entry_cap():
    repo = FakeRepo(tree=FakeTree([f"f{i}.py" for i in range(50)]))
    structure = gt.get_repo_structure("o/r", repo=repo, max_entries=4, sleep=_no_sleep)
    assert len(structure.entries) == 4
    assert structure.truncated is True


def test_repo_structure_not_truncated_when_within_caps():
    repo = FakeRepo(tree=FakeTree(["README.md", "src/a.py"]))
    structure = gt.get_repo_structure("o/r", repo=repo, sleep=_no_sleep)
    assert structure.truncated is False
    assert structure.entries[0].type == "blob"
    assert structure.entries[0].size == 10


def test_repo_structure_uses_default_branch_when_no_ref():
    repo = FakeRepo(tree=FakeTree(["a.py"]), default_branch="trunk")
    gt.get_repo_structure("o/r", repo=repo, sleep=_no_sleep)
    assert repo.tree_ref == "trunk"


def test_repo_structure_caps_read_from_env(monkeypatch):
    monkeypatch.setenv(gt.MAX_TREE_ENTRIES_ENV_VAR, "2")
    monkeypatch.setenv(gt.MAX_TREE_DEPTH_ENV_VAR, "1")
    repo = FakeRepo(tree=FakeTree(["a.py", "b.py", "c.py", "src/d.py"]))
    structure = gt.get_repo_structure("o/r", repo=repo, sleep=_no_sleep)
    assert structure.paths() == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# README / language / commits (REQ-2.1)
# ---------------------------------------------------------------------------


def test_get_readme_returns_text():
    repo = FakeRepo(readme=FakeContentFile(b"# Project\n"))
    assert gt.get_readme("o/r", repo=repo, sleep=_no_sleep) == "# Project\n"


def test_get_readme_missing_returns_none():
    repo = FakeRepo(readme_error=UnknownObjectException(404, {"message": "Not Found"}, {}))
    assert gt.get_readme("o/r", repo=repo, sleep=_no_sleep) is None


def test_get_primary_language():
    assert gt.get_primary_language("o/r", repo=FakeRepo(language="Rust"), sleep=_no_sleep) == "Rust"
    assert gt.get_primary_language("o/r", repo=FakeRepo(language=None), sleep=_no_sleep) is None


def test_recent_commits_capped_at_ten_by_default():
    repo = FakeRepo(commits=[FakeCommit(f"sha{i}") for i in range(25)])
    commits = gt.get_recent_commits("o/r", repo=repo, sleep=_no_sleep)
    assert gt.DEFAULT_COMMIT_LIMIT == 10
    assert len(commits) == 10
    assert [c.sha for c in commits] == [f"sha{i}" for i in range(10)]


def test_recent_commits_maps_fields():
    repo = FakeRepo(commits=[FakeCommit("abc123", message="fix: thing", author="Ada")])
    commit = gt.get_recent_commits("o/r", repo=repo, sleep=_no_sleep)[0]
    assert commit.sha == "abc123"
    assert commit.message == "fix: thing"
    assert commit.author == "Ada"
    assert commit.committed_at == "2022-05-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# gather_repo_context — REQ-2.1 coverage in one object
# ---------------------------------------------------------------------------


def _full_repo():
    return FakeRepo(
        issues=[FakeIssue(1, thumbs_up=3), FakeIssue(2, thumbs_up=8)],
        tree=FakeTree(["README.md", "src/a.py"]),
        readme=FakeContentFile(b"# Project"),
        commits=[FakeCommit(f"s{i}") for i in range(12)],
        language="Go",
        default_branch="main",
    )


def test_gather_repo_context_covers_every_req_2_1_read():
    context = gt.gather_repo_context("owner/repo", repo=_full_repo(), sleep=_no_sleep)
    # open issues, sorted by thumbs-up
    assert [i.number for i in context.issues] == [2, 1]
    # README
    assert context.readme == "# Project"
    # primary language
    assert context.primary_language == "Go"
    # folder structure
    assert context.structure.paths() == ["README.md", "src/a.py"]
    # last 10 commits
    assert len(context.recent_commits) == 10
    assert context.default_branch == "main"
    assert context.top_issue().number == 2


def test_gather_repo_context_is_json_serializable():
    import json

    context = gt.gather_repo_context("owner/repo", repo=_full_repo(), sleep=_no_sleep)
    payload = json.loads(json.dumps(context.to_dict()))
    assert payload["repo_full_name"] == "owner/repo"
    assert payload["issues"][0]["number"] == 2


def test_gather_repo_context_degrades_when_readme_read_fails():
    repo = _full_repo()
    repo._readme_error = RuntimeError("boom")
    context = gt.gather_repo_context("owner/repo", repo=repo, sleep=_no_sleep)
    assert context.readme is None
    # The reads that did work are still present.
    assert context.issues and context.recent_commits


def test_gather_repo_context_with_no_issues_has_no_top_issue():
    repo = FakeRepo(tree=FakeTree([]), readme=None, commits=[])
    context = gt.gather_repo_context("owner/repo", repo=repo, sleep=_no_sleep)
    assert context.issues == []
    assert context.top_issue() is None


# ---------------------------------------------------------------------------
# Rate-limit backoff (design.md section 10)
# ---------------------------------------------------------------------------


def test_rate_limit_backoff_is_applied_to_repo_lookup():
    slept = []
    client = FakeClient(
        _full_repo(),
        fail_times=1,
        exc=RateLimitExceededException(403, {"message": "rate limited"}, {}),
    )
    issues = gt.get_repo_issues(
        "owner/repo", client=client, sleep=lambda d: slept.append(d)
    )
    assert client.call_count == 2  # failed once, retried once
    assert [i.number for i in issues] == [2, 1]
    assert len(slept) == 1 and slept[0] > 0


def test_rate_limit_backoff_gives_up_and_raises():
    slept = []
    client = FakeClient(
        _full_repo(),
        fail_times=99,
        exc=RateLimitExceededException(403, {"message": "rate limited"}, {}),
    )
    with pytest.raises(RateLimitExceededException):
        gt.get_repo_issues("owner/repo", client=client, sleep=lambda d: slept.append(d))
    assert slept  # it did back off before giving up


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def test_resolve_client_prefers_injected_client():
    sentinel = object()
    assert gt.resolve_client(sentinel) is sentinel


def test_resolve_client_uses_env_token(monkeypatch):
    monkeypatch.setenv(gt.GITHUB_TOKEN_ENV_VAR, "ghp_fake")
    client = gt.resolve_client()
    from github import Github

    assert isinstance(client, Github)
