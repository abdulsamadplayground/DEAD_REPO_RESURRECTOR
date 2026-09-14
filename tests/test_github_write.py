"""Unit tests for src.tools.github_write (Engineer write tools).

Fully offline: fake PyGithub objects stand in for Repository / GitRef /
GitCommit / GitTree / ContentFile. No network, no AWS, no ``strands``.

Covers REQ-3.1 (branch named ``resurrector/fix-issue-{N}``, cut from the default
branch head, idempotent when it already exists), REQ-3.3 (syntax validation
before anything is pushed), REQ-3.4 (the commit lands on the fix branch), the
path-traversal defences, and rate-limit backoff (design.md section 10).
"""

from __future__ import annotations

import pytest
from github.GithubException import GithubException, RateLimitExceededException

import src.tools.github_write as gw


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeGitObject:
    def __init__(self, sha):
        self.sha = sha


class FakeGitRef:
    """Mimics a PyGithub GitRef, including the ``edit`` fast-forward."""

    def __init__(self, ref, sha):
        self.ref = ref
        self.object = FakeGitObject(sha)
        self.edits = []

    def edit(self, sha, force=None):
        self.edits.append(sha)
        self.object = FakeGitObject(sha)


class FakeGitTree:
    def __init__(self, sha="tree-sha"):
        self.sha = sha


class FakeGitCommit:
    def __init__(self, sha="commit-sha", tree=None):
        self.sha = sha
        self.tree = tree if tree is not None else FakeGitTree()
        self.html_url = f"https://github.com/o/r/commit/{sha}"


class FakeContentFile:
    def __init__(self, content=b"print('hi')\n", sha="blob-sha", raises=False):
        self._content = content
        self.sha = sha
        self._raises = raises

    @property
    def decoded_content(self):
        if self._raises:
            raise ValueError("cannot decode")
        return self._content


class FakeRepo:
    """The subset of a PyGithub Repository that github_write touches."""

    def __init__(
        self,
        *,
        default_branch="main",
        head_shas=None,
        existing_branches=(),
        contents=None,
        create_ref_error=None,
        create_file_error=None,
    ):
        self.default_branch = default_branch
        self.full_name = "owner/repo"
        # ref name (without "refs/") -> sha
        self._head_shas = head_shas or {"heads/main": "base-sha"}
        for name in existing_branches:
            self._head_shas.setdefault(f"heads/{name}", "existing-sha")
        self._existing_branches = set(existing_branches)
        self._contents = contents or {}
        self._create_ref_error = create_ref_error
        self._create_file_error = create_file_error

        self.created_refs = []
        self.created_files = []
        self.updated_files = []
        self.created_trees = []
        self.created_commits = []
        self.refs_handed_out = {}

    # -- refs ------------------------------------------------------------
    def get_git_ref(self, ref):
        if ref not in self._head_shas:
            raise GithubException(404, {"message": "Not Found"}, {})
        existing = self.refs_handed_out.get(ref)
        if existing is None:
            existing = FakeGitRef(ref, self._head_shas[ref])
            self.refs_handed_out[ref] = existing
        return existing

    def create_git_ref(self, ref, sha):
        if self._create_ref_error is not None:
            raise self._create_ref_error
        name = ref.removeprefix("refs/")
        if name.removeprefix("heads/") in self._existing_branches:
            raise GithubException(422, {"message": "Reference already exists"}, {})
        self.created_refs.append((ref, sha))
        self._head_shas[name] = sha
        return FakeGitRef(name, sha)

    # -- contents --------------------------------------------------------
    def get_contents(self, path, ref=None):
        if path not in self._contents:
            raise GithubException(404, {"message": "Not Found"}, {})
        return self._contents[path]

    def create_file(self, path, message, content, branch=None):
        if self._create_file_error is not None:
            error, self._create_file_error = self._create_file_error, None
            raise error
        self.created_files.append(
            {"path": path, "message": message, "content": content, "branch": branch}
        )
        return {"commit": FakeGitCommit("created-commit"), "content": FakeContentFile()}

    def update_file(self, path, message, content, sha, branch=None):
        self.updated_files.append(
            {
                "path": path,
                "message": message,
                "content": content,
                "sha": sha,
                "branch": branch,
            }
        )
        return {"commit": FakeGitCommit("updated-commit"), "content": FakeContentFile()}

    # -- git data --------------------------------------------------------
    def get_git_commit(self, sha):
        return FakeGitCommit(sha, FakeGitTree(f"tree-of-{sha}"))

    def create_git_tree(self, tree, base_tree=None):
        self.created_trees.append({"tree": tree, "base_tree": base_tree})
        return FakeGitTree("new-tree")

    def create_git_commit(self, message, tree, parents, **kwargs):
        self.created_commits.append(
            {"message": message, "tree": tree, "parents": parents}
        )
        return FakeGitCommit("pushed-commit", tree)


class FlakyRepo(FakeRepo):
    """Raises ``exc`` the first ``fail_times`` calls to a chosen method."""

    def __init__(self, method, exc, fail_times=1, **kwargs):
        super().__init__(**kwargs)
        self._method = method
        self._exc = exc
        self._remaining = fail_times
        self.attempts = 0

    def _maybe_fail(self, name):
        if name == self._method:
            self.attempts += 1
            if self._remaining > 0:
                self._remaining -= 1
                raise self._exc

    def create_git_tree(self, tree, base_tree=None):
        self._maybe_fail("create_git_tree")
        return super().create_git_tree(tree, base_tree)

    def create_file(self, path, message, content, branch=None):
        self._maybe_fail("create_file")
        return super().create_file(path, message, content, branch=branch)


def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.env",
        "src/../../etc/passwd",
        "/etc/passwd",
        "/absolute/file.py",
        "..",
        "a/b/../../../c",
        "..\\windows\\system32",
        "C:/Windows/system.ini",
        ".git/config",
        ".github/workflows/ci.yml",
        " leading.py",
        "trailing.py ",
        "nul\x00byte.py",
        "",
        "   ",
        "./",
        123,
        None,
    ],
)
def test_normalize_repo_path_rejects_escapes(path):
    with pytest.raises(gw.PathTraversalError):
        gw.normalize_repo_path(path)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("src/parser.py", "src/parser.py"),
        ("./src/parser.py", "src/parser.py"),
        ("src//parser.py", "src/parser.py"),
        ("src\\parser.py", "src/parser.py"),
        ("README.md", "README.md"),
        ("a/b/c/d.py", "a/b/c/d.py"),
        (".gitignore", ".gitignore"),
    ],
)
def test_normalize_repo_path_accepts_and_cleans(given, expected):
    assert gw.normalize_repo_path(given) == expected


def test_file_change_normalizes_its_path():
    change = gw.FileChange(path="./src/x.py", content="x = 1\n")
    assert change.path == "src/x.py"


def test_file_change_rejects_a_traversal_path():
    with pytest.raises(gw.PathTraversalError):
        gw.FileChange(path="../x.py", content="x = 1\n")


# ---------------------------------------------------------------------------
# REQ-3.1 — branch creation
# ---------------------------------------------------------------------------


def test_fix_branch_name_matches_the_requirement():
    assert gw.fix_branch_name(42) == "resurrector/fix-issue-42"


@pytest.mark.parametrize("bad", [0, -1, "7", None, True, 3.5])
def test_fix_branch_name_rejects_a_bad_issue_number(bad):
    with pytest.raises(ValueError):
        gw.fix_branch_name(bad)


def test_create_branch_uses_the_default_branch_head_sha():
    repo = FakeRepo(default_branch="trunk", head_shas={"heads/trunk": "trunk-head"})
    branch = gw.create_branch("owner/repo", 42, repo=repo, sleep=_no_sleep)

    assert branch.name == "resurrector/fix-issue-42"
    assert branch.ref == "refs/heads/resurrector/fix-issue-42"
    assert branch.base_branch == "trunk"
    assert branch.base_sha == "trunk-head"
    assert branch.created is True
    # REQ-3.1: cut from the DEFAULT branch's head, not from an arbitrary ref.
    assert repo.created_refs == [
        ("refs/heads/resurrector/fix-issue-42", "trunk-head")
    ]


def test_create_branch_honours_an_explicit_base():
    repo = FakeRepo(
        default_branch="main",
        head_shas={"heads/main": "main-head", "heads/develop": "dev-head"},
    )
    branch = gw.create_branch(
        "owner/repo", 7, repo=repo, base_branch="develop", sleep=_no_sleep
    )
    assert branch.base_branch == "develop"
    assert repo.created_refs == [("refs/heads/resurrector/fix-issue-7", "dev-head")]


def test_create_branch_is_idempotent_when_the_branch_exists():
    repo = FakeRepo(existing_branches=("resurrector/fix-issue-9",))
    branch = gw.create_branch("owner/repo", 9, repo=repo, sleep=_no_sleep)

    assert branch.name == "resurrector/fix-issue-9"
    assert branch.created is False
    assert branch.sha == "existing-sha"
    assert repo.created_refs == []  # nothing was created the second time


def test_create_branch_propagates_a_non_422_error():
    repo = FakeRepo(create_ref_error=GithubException(500, {"message": "boom"}, {}))
    with pytest.raises(GithubException):
        gw.create_branch("owner/repo", 3, repo=repo, sleep=_no_sleep)


def test_create_branch_needs_a_default_branch():
    repo = FakeRepo(default_branch=None)
    with pytest.raises(ValueError, match="default branch"):
        gw.create_branch("owner/repo", 3, repo=repo, sleep=_no_sleep)


# ---------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------


def test_get_file_returns_text_and_blob_sha():
    repo = FakeRepo(
        contents={"src/x.py": FakeContentFile(b"x = 1\n", sha="abc123")}
    )
    result = gw.get_file("owner/repo", "src/x.py", ref="main", repo=repo, sleep=_no_sleep)
    assert result.exists is True
    assert result.text == "x = 1\n"
    assert result.sha == "abc123"
    assert result.truncated is False


def test_get_file_reports_a_missing_file_without_raising():
    repo = FakeRepo(contents={})
    result = gw.get_file("owner/repo", "src/nope.py", repo=repo, sleep=_no_sleep)
    assert result.exists is False
    assert result.sha is None
    assert result.text is None


def test_get_file_flags_truncation():
    repo = FakeRepo(contents={"big.py": FakeContentFile(b"a" * 100, sha="s")})
    result = gw.get_file("owner/repo", "big.py", repo=repo, max_bytes=10, sleep=_no_sleep)
    assert result.truncated is True
    assert result.text.startswith("aaaaaaaaaa")
    assert "truncated by resurrector" in result.text


def test_get_file_on_a_directory_returns_no_text():
    repo = FakeRepo(contents={"src": [FakeContentFile()]})
    result = gw.get_file("owner/repo", "src", repo=repo, sleep=_no_sleep)
    assert result.exists is True
    assert result.text is None


def test_get_file_validates_the_path():
    with pytest.raises(gw.PathTraversalError):
        gw.get_file("owner/repo", "../etc/passwd", repo=FakeRepo(), sleep=_no_sleep)


# ---------------------------------------------------------------------------
# write_file — create vs update
# ---------------------------------------------------------------------------


def test_write_file_creates_on_the_fix_branch_when_no_sha_is_given():
    repo = FakeRepo()
    result = gw.write_file(
        "owner/repo",
        "src/new.py",
        "x = 1\n",
        branch="resurrector/fix-issue-1",
        message="add new",
        repo=repo,
        sleep=_no_sleep,
    )
    assert len(repo.created_files) == 1
    # The branch is always threaded through; never left to default to main.
    assert repo.created_files[0]["branch"] == "resurrector/fix-issue-1"
    assert repo.created_files[0]["path"] == "src/new.py"
    assert repo.updated_files == []
    assert result.api == "contents"
    assert result.commit_sha == "created-commit"
    assert result.files == ["src/new.py"]


def test_write_file_updates_when_a_sha_is_given():
    repo = FakeRepo()
    result = gw.write_file(
        "owner/repo",
        "src/x.py",
        "x = 2\n",
        branch="resurrector/fix-issue-1",
        message="bump",
        sha="blob-sha",
        repo=repo,
        sleep=_no_sleep,
    )
    assert repo.created_files == []
    assert repo.updated_files[0]["sha"] == "blob-sha"
    assert repo.updated_files[0]["branch"] == "resurrector/fix-issue-1"
    assert result.commit_sha == "updated-commit"


def test_write_file_recovers_when_a_create_hits_an_existing_file():
    # GitHub answers 422 when the file exists and the request omitted its SHA.
    repo = FakeRepo(
        contents={"src/x.py": FakeContentFile(b"old\n", sha="found-sha")},
        create_file_error=GithubException(422, {"message": "sha wasn't supplied"}, {}),
    )
    result = gw.write_file(
        "owner/repo",
        "src/x.py",
        "new\n",
        branch="fixbranch",
        message="m",
        repo=repo,
        sleep=_no_sleep,
    )
    assert repo.updated_files[0]["sha"] == "found-sha"
    assert result.commit_sha == "updated-commit"


def test_write_file_requires_a_branch():
    with pytest.raises(ValueError, match="branch is required"):
        gw.write_file(
            "owner/repo", "x.py", "x", branch="", message="m", repo=FakeRepo()
        )


def test_write_file_rejects_a_traversal_path():
    with pytest.raises(gw.PathTraversalError):
        gw.write_file(
            "owner/repo",
            "../../etc/passwd",
            "pwned",
            branch="b",
            message="m",
            repo=FakeRepo(),
        )


def test_write_file_rejects_an_oversized_file():
    with pytest.raises(ValueError, match="over the"):
        gw.write_file(
            "owner/repo",
            "big.py",
            "a" * 100,
            branch="b",
            message="m",
            max_bytes=10,
            repo=FakeRepo(),
        )


# ---------------------------------------------------------------------------
# REQ-3.4 — push_commit
# ---------------------------------------------------------------------------


def _branch_repo(branch="resurrector/fix-issue-5"):
    return FakeRepo(head_shas={"heads/main": "base", f"heads/{branch}": "parent-sha"})


def test_push_commit_lands_one_commit_on_the_fix_branch():
    branch = "resurrector/fix-issue-5"
    repo = _branch_repo(branch)
    result = gw.push_commit(
        "owner/repo",
        [
            {"path": "src/a.py", "content": "a = 1\n"},
            {"path": "src/b.py", "content": "b = 2\n"},
        ],
        branch=branch,
        message="Fix issue #5",
        repo=repo,
        sleep=_no_sleep,
    )

    # One tree, one commit, one ref move — for a two-file change (atomicity).
    assert len(repo.created_trees) == 1
    assert len(repo.created_commits) == 1
    assert repo.refs_handed_out[f"heads/{branch}"].edits == ["pushed-commit"]

    # The tree was built on top of the branch head's tree, with both blobs.
    tree_call = repo.created_trees[0]
    assert tree_call["base_tree"].sha == "tree-of-parent-sha"
    assert {element._identity["path"] for element in tree_call["tree"]} == {
        "src/a.py",
        "src/b.py",
    }
    assert {element._identity["mode"] for element in tree_call["tree"]} == {"100644"}
    assert {element._identity["type"] for element in tree_call["tree"]} == {"blob"}

    # The commit's parent is the previous branch head.
    assert repo.created_commits[0]["parents"][0].sha == "parent-sha"
    assert repo.created_commits[0]["message"] == "Fix issue #5"

    assert result.api == "git-trees"
    assert result.branch == branch
    assert result.commit_sha == "pushed-commit"
    assert result.files == ["src/a.py", "src/b.py"]


def test_push_commit_accepts_file_change_objects():
    branch = "resurrector/fix-issue-5"
    repo = _branch_repo(branch)
    result = gw.push_commit(
        "owner/repo",
        [gw.FileChange("src/a.py", "a = 1\n")],
        branch=branch,
        message="m",
        repo=repo,
        sleep=_no_sleep,
    )
    assert result.files == ["src/a.py"]


def test_push_commit_requires_a_message():
    with pytest.raises(ValueError, match="commit message"):
        gw.push_commit(
            "owner/repo",
            [{"path": "a.py", "content": "a"}],
            branch="b",
            message="  ",
            repo=FakeRepo(),
        )


def test_push_commit_rejects_a_traversal_path():
    with pytest.raises(gw.PathTraversalError):
        gw.push_commit(
            "owner/repo",
            [{"path": "../evil.py", "content": "x"}],
            branch="resurrector/fix-issue-5",
            message="m",
            repo=_branch_repo(),
        )


def test_push_commit_fails_when_the_branch_is_missing():
    repo = FakeRepo()  # only heads/main exists
    with pytest.raises(GithubException):
        gw.push_commit(
            "owner/repo",
            [{"path": "a.py", "content": "a"}],
            branch="resurrector/fix-issue-99",
            message="m",
            repo=repo,
            sleep=_no_sleep,
        )


# ---------------------------------------------------------------------------
# coerce_changes — bounds and shapes
# ---------------------------------------------------------------------------


def test_coerce_changes_accepts_dicts_and_objects():
    changes = gw.coerce_changes(
        [{"path": "a.py", "content": "a"}, gw.FileChange("b.py", "b")]
    )
    assert [c.path for c in changes] == ["a.py", "b.py"]


def test_coerce_changes_rejects_an_empty_set():
    with pytest.raises(ValueError, match="empty"):
        gw.coerce_changes([])


def test_coerce_changes_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate"):
        gw.coerce_changes(
            [{"path": "a.py", "content": "1"}, {"path": "./a.py", "content": "2"}]
        )


def test_coerce_changes_enforces_the_file_cap():
    many = [{"path": f"f{i}.py", "content": "x"} for i in range(5)]
    with pytest.raises(ValueError, match="over the 3-file limit"):
        gw.coerce_changes(many, max_files=3)


def test_coerce_changes_enforces_the_byte_cap():
    with pytest.raises(ValueError, match="over the 10-byte limit"):
        gw.coerce_changes([{"path": "a.py", "content": "x" * 50}], max_bytes=10)


def test_coerce_changes_rejects_a_missing_content_key():
    with pytest.raises(ValueError, match="'path' and 'content'"):
        gw.coerce_changes([{"path": "a.py"}])


def test_coerce_changes_rejects_an_unsupported_entry():
    with pytest.raises(ValueError, match="unsupported change entry"):
        gw.coerce_changes(["a.py"])


# ---------------------------------------------------------------------------
# REQ-3.3 — syntax validation
# ---------------------------------------------------------------------------


def test_valid_python_passes_validation():
    check = gw.validate_syntax("src/x.py", "def f():\n    return 1\n")
    assert check.validated is True
    assert check.ok is True
    assert check.language == "python"


def test_invalid_python_is_detected():
    check = gw.validate_syntax("src/x.py", "def f(:\n    return 1\n")
    assert check.validated is True
    assert check.ok is False
    assert "SyntaxError" in check.error


def test_python_with_a_nul_byte_is_detected():
    check = gw.validate_syntax("src/x.py", "x = 1\x00\n")
    assert check.ok is False


def test_invalid_json_is_detected():
    check = gw.validate_syntax("data.json", "{not: json,}")
    assert check.validated is True
    assert check.ok is False
    assert check.language == "json"


def test_valid_json_passes_validation():
    assert gw.validate_syntax("data.json", '{"a": 1}').ok is True


def test_other_languages_are_reported_as_unvalidated_not_as_valid():
    # The honest-limitation path: we do not have a Go parser, and we say so
    # rather than implying we checked.
    check = gw.validate_syntax("main.go", "func main() { this is not go")
    assert check.validated is False
    assert check.language is None
    # ok=True means "no problem found", and validated=False is what tells the
    # caller not to read that as verification.
    assert check.ok is True


def test_syntax_failures_filters_to_real_failures():
    checks = [
        gw.validate_syntax("a.py", "x = 1\n"),
        gw.validate_syntax("b.py", "def ("),
        gw.validate_syntax("c.go", "garbage"),
    ]
    failures = gw.syntax_failures(checks)
    assert [f.path for f in failures] == ["b.py"]


def test_validate_changes_covers_every_file():
    changes = [gw.FileChange("a.py", "x = 1\n"), gw.FileChange("b.json", "[]")]
    assert len(gw.validate_changes(changes)) == 2


# ---------------------------------------------------------------------------
# Rate-limit backoff (design.md section 10)
# ---------------------------------------------------------------------------


def test_push_commit_retries_after_a_rate_limit():
    slept = []
    repo = FlakyRepo(
        "create_git_tree",
        RateLimitExceededException(403, {"message": "rate limited"}, {}),
        fail_times=1,
        head_shas={"heads/main": "base", "heads/fixb": "parent-sha"},
    )
    result = gw.push_commit(
        "owner/repo",
        [{"path": "a.py", "content": "a = 1\n"}],
        branch="fixb",
        message="m",
        repo=repo,
        sleep=slept.append,
    )
    assert result.commit_sha == "pushed-commit"
    assert repo.attempts == 2  # failed once, then succeeded
    assert len(slept) == 1 and slept[0] > 0


def test_write_file_retries_after_a_rate_limit():
    slept = []
    repo = FlakyRepo(
        "create_file",
        RateLimitExceededException(403, {"message": "rate limited"}, {}),
        fail_times=1,
    )
    result = gw.write_file(
        "owner/repo",
        "a.py",
        "a = 1\n",
        branch="fixb",
        message="m",
        repo=repo,
        sleep=slept.append,
    )
    assert result.commit_sha == "created-commit"
    assert repo.attempts == 2
    assert len(slept) == 1


def test_push_commit_gives_up_after_exhausting_retries():
    repo = FlakyRepo(
        "create_git_tree",
        RateLimitExceededException(403, {"message": "rate limited"}, {}),
        fail_times=99,
        head_shas={"heads/main": "base", "heads/fixb": "parent-sha"},
    )
    with pytest.raises(RateLimitExceededException):
        gw.push_commit(
            "owner/repo",
            [{"path": "a.py", "content": "a = 1\n"}],
            branch="fixb",
            message="m",
            repo=repo,
            sleep=_no_sleep,
        )


# ---------------------------------------------------------------------------
# Boundary — this module writes code, and nothing else
# ---------------------------------------------------------------------------


def test_github_write_does_not_open_prs_or_post_comments():
    # design.md section 3: the Communicator is the only agent that opens PRs or
    # posts comments. The Engineer's write layer must not be able to.
    import inspect

    source = inspect.getsource(gw)
    assert "create_pull" not in source
    assert "create_issue_comment" not in source
    assert "create_comment" not in source


def test_github_write_does_not_touch_dynamodb():
    import inspect

    source = inspect.getsource(gw)
    assert "dynamo_tools" not in source
    assert "write_state" not in source
