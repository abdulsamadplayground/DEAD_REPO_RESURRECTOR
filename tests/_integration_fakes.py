"""Shared fakes for the offline integration tests (Task 12).

These stand in for the two outermost seams the integration suite deliberately
does NOT touch for real: the GitHub API (PyGithub ``Repository`` / ``Issue`` /
``PullRequest`` objects) and the Strands/Bedrock model. Everything else — the
DynamoDB state machine, the Analyst/Engineer/Communicator pipeline, the PR
templates, the reply classifier, the follow-up scheduler — runs the real
production code. AWS is mocked with moto (DynamoDB) or injected recorders
(SQS/SNS in the Processor).

The single :class:`FakeRepo` here is intentionally richer than any one unit
test's local fake: it must satisfy, in one object threaded through a whole
``orchestrator.run`` pipeline, the reads of :mod:`src.tools.github_tools`, the
writes of :mod:`src.tools.github_write`, and the comms of
:mod:`src.tools.github_comms`. It borrows the shapes the unit-test fakes already
established (see ``tests/test_github_tools.py``, ``tests/test_github_write.py``,
``tests/test_github_comms.py``) rather than inventing new ones.

Real AWS/GitHub/Bedrock validation is Task 13; nothing here touches a network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Optional

from github.GithubException import GithubException

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Small leaf objects (mirrors of PyGithub sub-objects the code touches)
# ---------------------------------------------------------------------------


class FakeLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeComment:
    def __init__(self, comment_id: int, html_url: str) -> None:
        self.id = comment_id
        self.html_url = html_url


class FakeContentFile:
    """A file blob: ``decoded_content`` bytes plus a blob ``sha``."""

    def __init__(self, content: bytes = b"", sha: str = "blob-sha") -> None:
        self._content = content
        self.sha = sha

    @property
    def decoded_content(self) -> bytes:
        return self._content


class FakeGitObject:
    def __init__(self, sha: str) -> None:
        self.sha = sha


class FakeGitRef:
    """A git ref, including the ``edit`` fast-forward used by push_commit."""

    def __init__(self, ref: str, sha: str) -> None:
        self.ref = ref
        self.object = FakeGitObject(sha)
        self.edits: list[str] = []

    def edit(self, sha: str, force: Optional[bool] = None) -> None:
        self.edits.append(sha)
        self.object = FakeGitObject(sha)


class FakeTreeElement:
    def __init__(self, path: str, type: str = "blob", size: int = 0) -> None:
        self.path = path
        self.type = type
        self.size = size


class FakeGitTree:
    def __init__(self, sha: str = "tree-sha") -> None:
        self.sha = sha
        self.tree: list[FakeTreeElement] = []
        self.truncated = False


class FakeGitCommit:
    def __init__(self, sha: str = "commit-sha", tree: Optional[FakeGitTree] = None) -> None:
        self.sha = sha
        self.tree = tree if tree is not None else FakeGitTree()
        self.html_url = f"https://github.com/owner/repo/commit/{sha}"


class FakeRepoCommit:
    """A commit as returned by ``get_commits`` (top-level + nested ``commit``)."""

    def __init__(self, sha: str, message: str, author_name: str, date: datetime) -> None:
        self.sha = sha
        self.commit = SimpleNamespace(
            message=message,
            author=SimpleNamespace(name=author_name, date=date),
        )
        self.author = SimpleNamespace(login=author_name)
        self.html_url = f"https://github.com/owner/repo/commit/{sha}"


class FakeHead:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class FakePull:
    def __init__(self, number: int, head_ref: str, html_url: Optional[str] = None) -> None:
        self.number = number
        self.head = FakeHead(head_ref)
        self.html_url = html_url or f"https://github.com/owner/repo/pull/{number}"


class FakeIssue:
    """An issue (and, since a PR is an issue, the comment surface for PRs too).

    ``create_comment`` records the body so a test can assert what a maintainer
    actually reads (REQ-4.3 issue comment, REQ-5.3 reply, REQ-6.2 follow-up).
    """

    def __init__(
        self,
        number: int,
        *,
        title: str = "",
        body: str = "",
        thumbs_up: int = 0,
        total_reactions: int = 0,
        comments: int = 0,
        labels: tuple[str, ...] = (),
        is_pr: bool = False,
        repo_full_name: str = "owner/repo",
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        # Inline reactions summary — github_tools reads this with zero extra calls.
        self.reactions = {"+1": thumbs_up, "total_count": total_reactions}
        self.comments = comments
        self.labels = [FakeLabel(name) for name in labels]
        self.created_at = datetime(2021, 1, 1, tzinfo=UTC)
        self.updated_at = datetime(2021, 6, 1, tzinfo=UTC)
        self.html_url = f"https://github.com/{repo_full_name}/issues/{number}"
        self.pull_request = object() if is_pr else None
        self.posted_comments: list[str] = []

    def create_comment(self, body: str) -> FakeComment:
        self.posted_comments.append(body)
        comment_id = 1000 + len(self.posted_comments)
        return FakeComment(comment_id, f"{self.html_url}#issuecomment-{comment_id}")


# ---------------------------------------------------------------------------
# The unified repository fake
# ---------------------------------------------------------------------------


class FakeRepo:
    """One repository fake covering reads, code writes, and PR/comment writes.

    Reads (github_tools): ``get_issues``, ``get_readme``, ``get_git_tree``,
    ``get_commits``, plus ``default_branch`` / ``language`` attributes and
    ``get_contents`` for file reads.

    Code writes (github_write): ``get_git_ref`` / ``create_git_ref`` (branch),
    ``get_git_commit`` / ``create_git_tree`` / ``create_git_commit`` and
    ``FakeGitRef.edit`` (the atomic trees-API push).

    Comms (github_comms): ``create_pull`` / ``get_pulls`` (open the PR) and
    ``get_issue`` (post issue/PR comments).
    """

    def __init__(
        self,
        *,
        full_name: str = "owner/repo",
        default_branch: str = "main",
        language: str = "Python",
        issues: Optional[list[FakeIssue]] = None,
        readme: Optional[str] = "# Project\n\nA small library.\n",
        tree_paths: tuple[str, ...] = ("README.md", "loader.py", "tests/test_loader.py"),
        contents: Optional[dict[str, FakeContentFile]] = None,
        base_sha: str = "base-sha",
    ) -> None:
        self.full_name = full_name
        self.default_branch = default_branch
        self.language = language
        self._readme = readme
        self._tree_paths = tree_paths
        self._contents = contents or {}

        # Issues: keyed by number for get_issue(); listed for get_issues().
        self._issue_list = issues if issues is not None else []
        self._issues_by_number: dict[int, FakeIssue] = {
            issue.number: issue for issue in self._issue_list
        }

        # ref name (without "refs/") -> sha
        self._head_shas: dict[str, str] = {f"heads/{default_branch}": base_sha}
        self._refs_handed_out: dict[str, FakeGitRef] = {}

        # PR bookkeeping
        self._pulls: list[FakePull] = []
        self._next_pr_number = 321

        # Recording surfaces for assertions
        self.created_refs: list[tuple[str, str]] = []
        self.created_trees: list[dict[str, Any]] = []
        self.created_commits: list[dict[str, Any]] = []
        self.created_pulls: list[dict[str, Any]] = []

    # -- reads ----------------------------------------------------------
    def get_issues(self, state: Optional[str] = None):
        return list(self._issue_list)

    def get_readme(self, ref: Optional[str] = None):
        if self._readme is None:
            raise GithubException(404, {"message": "Not Found"}, {})
        return FakeContentFile(self._readme.encode("utf-8"), sha="readme-sha")

    def get_git_tree(self, sha: str, recursive: bool = False):
        tree = FakeGitTree(f"tree-of-{sha}")
        tree.tree = [FakeTreeElement(path) for path in self._tree_paths]
        return tree

    def get_commits(self):
        return [
            FakeRepoCommit(
                sha=f"c{i}",
                message=f"Earlier work {i}",
                author_name="maintainer",
                date=datetime(2022, 1, i + 1, tzinfo=UTC),
            )
            for i in range(3)
        ]

    def get_contents(self, path: str, ref: Optional[str] = None):
        if path not in self._contents:
            raise GithubException(404, {"message": "Not Found"}, {})
        return self._contents[path]

    # -- code writes (branch + trees push) ------------------------------
    def get_git_ref(self, ref: str) -> FakeGitRef:
        if ref not in self._head_shas:
            raise GithubException(404, {"message": "Not Found"}, {})
        existing = self._refs_handed_out.get(ref)
        if existing is None:
            existing = FakeGitRef(ref, self._head_shas[ref])
            self._refs_handed_out[ref] = existing
        return existing

    def create_git_ref(self, ref: str, sha: str) -> FakeGitRef:
        name = ref.removeprefix("refs/")
        if name in self._head_shas:
            raise GithubException(422, {"message": "Reference already exists"}, {})
        self.created_refs.append((ref, sha))
        self._head_shas[name] = sha
        return FakeGitRef(name, sha)

    def get_git_commit(self, sha: str) -> FakeGitCommit:
        return FakeGitCommit(sha, FakeGitTree(f"tree-of-{sha}"))

    def create_git_tree(self, tree, base_tree=None) -> FakeGitTree:
        self.created_trees.append({"tree": tree, "base_tree": base_tree})
        return FakeGitTree("new-tree")

    def create_git_commit(self, message, tree, parents, **kwargs) -> FakeGitCommit:
        self.created_commits.append(
            {"message": message, "tree": tree, "parents": parents}
        )
        return FakeGitCommit("pushed-commit", tree)

    # -- comms (open PR + comments) -------------------------------------
    def create_pull(self, title, body, head, base) -> FakePull:
        pull = FakePull(number=self._next_pr_number, head_ref=head)
        self._next_pr_number += 1
        self._pulls.append(pull)
        self.created_pulls.append(
            {"title": title, "body": body, "head": head, "base": base}
        )
        return pull

    def get_pulls(self, state: Optional[str] = None):
        return list(self._pulls)

    def get_issue(self, number: int) -> FakeIssue:
        issue = self._issues_by_number.get(int(number))
        if issue is None:
            issue = FakeIssue(int(number), repo_full_name=self.full_name)
            self._issues_by_number[int(number)] = issue
        return issue

    # -- test helpers ---------------------------------------------------
    def comments_on(self, number: int) -> list[str]:
        """All comment bodies posted to the issue/PR ``number`` (empty if none)."""
        issue = self._issues_by_number.get(int(number))
        return list(issue.posted_comments) if issue is not None else []

    def branch_exists(self, name: str) -> bool:
        return f"heads/{name}" in self._head_shas


class FakeGithubClient:
    """A minimal ``github.Github`` stand-in whose ``get_repo`` returns ``repo``."""

    def __init__(self, repo: FakeRepo) -> None:
        self._repo = repo
        self.get_repo_calls: list[str] = []

    def get_repo(self, full_name: str) -> FakeRepo:
        self.get_repo_calls.append(full_name)
        return self._repo


# ---------------------------------------------------------------------------
# Canned model (Strands/Bedrock) — a single run_model that serves every prompt
# ---------------------------------------------------------------------------


def make_model(
    *,
    analyst_json: Optional[dict[str, Any]] = None,
    engineer_json: Optional[dict[str, Any]] = None,
    reply_text: str = "The default is preserved for empty input; see the loader change.",
) -> Callable[[str], str]:
    """Build a deterministic ``run_model`` that dispatches on the prompt text.

    The Analyst, the Engineer, and the reply-drafting path all funnel through a
    single ``Callable[[str], str]`` model seam. Their prompts carry distinct,
    stable sentinel phrases (see the prompt builders), so one canned model can
    serve all three deterministically without any real Bedrock call.
    """
    analyst_payload = analyst_json if analyst_json is not None else {
        "complexity": "trivial",
        "confidence": 0.9,
        "files_affected": ["loader.py"],
        "approach": "Guard the empty-config case in loader.py and add a test.",
    }
    engineer_payload = engineer_json if engineer_json is not None else {
        "summary": "Return an empty dict for empty configs instead of crashing.",
        "commit_message": "Fix crash on empty config",
        "files": [
            {
                "path": "loader.py",
                "content": (
                    "def load(config):\n"
                    "    if not config:\n"
                    "        return {}\n"
                    "    return dict(config)\n"
                ),
            }
        ],
        "confident": True,
    }

    def run_model(prompt: str) -> str:
        if "Write the smallest correct fix" in prompt:
            return json.dumps(engineer_payload)
        if "Score this issue now" in prompt:
            return json.dumps(analyst_payload)
        if "Draft a short" in prompt:
            return reply_text
        # Unknown prompt: return something inert and unparsable as either verdict.
        return "OK"

    return run_model


# ---------------------------------------------------------------------------
# Recording AWS clients for the Processor (SQS / SNS) — no moto needed
# ---------------------------------------------------------------------------


class RecordingSqs:
    """Captures ``send_message`` kwargs instead of talking to SQS."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": f"msg-{len(self.sent)}"}


class RecordingSns:
    """Captures ``publish`` kwargs instead of talking to SNS."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": f"sns-{len(self.published)}"}
