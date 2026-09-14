"""GitHub **write** operations backing the Engineer sub-agent.

The Engineer is the only agent that writes code or creates branches (design.md
section 3). This module holds the plain, framework-free functions behind its
GitHub-mutating tools:

- :func:`create_branch` — ``resurrector/fix-issue-{N}`` off the default branch (REQ-3.1)
- :func:`get_file` — a file's text **and its blob SHA** (the SHA is what the
  Contents API needs in order to update an existing file)
- :func:`write_file` — create-or-update one file on the fix branch (REQ-3.4)
- :func:`push_commit` — one atomic multi-file commit on the fix branch (REQ-3.4)
- :func:`validate_changes` — syntax validation before anything is pushed (REQ-3.3)

Why this is a separate module from :mod:`src.tools.github_tools`
---------------------------------------------------------------
:mod:`src.tools.github_tools` is the Analyst's surface and its docstring states
it is *strictly read-only* — "there is no code path that creates, edits, or
deletes anything". That claim is a boundary the design leans on (design.md
section 3: "Analyst is strictly read-only against GitHub"), and it is worth
keeping mechanically checkable: with reads and writes in separate modules, the
Analyst/Engineer boundary can be audited by looking at imports rather than by
reading every function. Bolting ``create_branch`` onto the read module would
have forced us to weaken that docstring and would have put mutating functions
one autocomplete away from read-only code. Two modules, one direction of
mutation each.

The two modules do share :func:`src.tools.github_tools.resolve_client`. That
function builds an authenticated ``Github`` client (injected client → explicit
token → ``GITHUB_TOKEN`` → the Secrets-Manager loader cached per container); it
performs no reads itself, and duplicating a third token resolver would be worse
than the shared import.

Design references
-----------------
- Section 6: writes go through the GitHub REST API (branch refs, file-contents
  commits). Auth is a fine-grained PAT from Secrets Manager, cached per
  container.
- Section 10: rate-limit errors trigger exponential backoff. Every call here is
  wrapped in :func:`src.tools.github_search.with_backoff` — the single backoff
  implementation in this codebase, deliberately not re-written.

No ``strands`` import lives here. The ``@tool`` adapters are in
:mod:`src.agents.engineer`; everything below is an ordinary function so it stays
importable and testable without the agent framework or a network.

``push_commit`` uses the git *trees* API, not the Contents API
--------------------------------------------------------------
See :func:`push_commit` for the full reasoning. Short version: a fix that spans
two files must land as **one** commit, so the branch is never observed in a
half-applied state and so the "tests passed" claim from REQ-3.2 refers to
exactly the tree that got pushed. The Contents API can only commit one file per
call. :func:`write_file` still uses the Contents API, because a single-file
create-or-update is precisely what that endpoint is for and it is the tool shape
design.md section 3 lists for the Engineer.

Configuration
-------------
- ``RESURRECTOR_MAX_FILE_BYTES`` — per-file read cap (default 65536, shared with
  the read module)
- ``RESURRECTOR_MAX_CHANGED_FILES`` — files one fix may touch (default 20)
- ``RESURRECTOR_MAX_WRITE_BYTES`` — bytes one changed file may contain
  (default 262144)
"""

from __future__ import annotations

import ast
import json
import logging
import os
import posixpath
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from github import Github
from github.GithubException import GithubException, UnknownObjectException
from github.InputGitTreeElement import InputGitTreeElement

from src.tools.github_search import with_backoff
from src.tools.github_tools import (
    DEFAULT_MAX_FILE_BYTES,
    MAX_FILE_BYTES_ENV_VAR,
    TRUNCATION_MARKER,
    resolve_client,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: REQ-3.1 fixes the branch name exactly. This is the requirement, not a knob.
BRANCH_NAME_TEMPLATE = "resurrector/fix-issue-{number}"

DEFAULT_MAX_CHANGED_FILES = 20
DEFAULT_MAX_WRITE_BYTES = 256 * 1024

MAX_CHANGED_FILES_ENV_VAR = "RESURRECTOR_MAX_CHANGED_FILES"
MAX_WRITE_BYTES_ENV_VAR = "RESURRECTOR_MAX_WRITE_BYTES"

#: Git file mode for a normal, non-executable blob. The trees API requires an
#: explicit mode; we never create symlinks (``120000``) or submodule links
#: (``160000``) — both would be a way to smuggle something past review.
BLOB_MODE = "100644"

#: HTTP 422 is what GitHub returns both for "Reference already exists" on
#: ``POST /git/refs`` and for a Contents-API update that omitted the blob SHA.
_UNPROCESSABLE = 422


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid.

    Mirrors the ``_env_*`` convention used across the other tool modules.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class PathTraversalError(ValueError):
    """Raised when a proposed repository path escapes the repository root."""


class SyntaxInvalidError(ValueError):
    """Raised when a proposed change is not syntactically valid (REQ-3.3)."""


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

#: Paths under these prefixes are refused outright. ``.git`` is repository
#: metadata (writing there via the API is nonsense at best); the workflow
#: directories are refused because a fine-grained PAT that could write them
#: would let a generated "fix" alter CI — far outside what REQ-3 asks for.
FORBIDDEN_PREFIXES = (".git/", ".github/workflows/")


def normalize_repo_path(path: Any) -> str:
    """Validate and normalize a repository-relative path.

    Returns the cleaned POSIX path. Raises :class:`PathTraversalError` for
    anything that is not plainly inside the repository root:

    - a non-string, empty, or whitespace-only path
    - an absolute path (``/etc/passwd``) or a Windows drive/UNC path (``C:\\...``)
    - any ``..`` segment, before or after normalization
    - a NUL byte or a leading/trailing space (both are ways to make two paths
      look identical to a reviewer while differing on disk)
    - anything under :data:`FORBIDDEN_PREFIXES`

    Tolerates and cleans: ``./`` prefixes, duplicated slashes, and backslashes
    used as separators (normalized to ``/`` before validation, so a
    ``..\\..\\x`` attempt is caught rather than passed through as a filename).

    This is string-level validation, which is the right level for the Contents
    and trees APIs — they take a path, not a filesystem handle, so there is no
    local symlink to follow. The separate filesystem-level containment check for
    the local test checkout lives in :func:`src.tools.suite_runner.safe_join`,
    where ``realpath`` is the correct tool.
    """
    if not isinstance(path, str):
        raise PathTraversalError(f"path must be a string, got {type(path).__name__}")
    if "\x00" in path:
        raise PathTraversalError("path contains a NUL byte")
    if path != path.strip():
        raise PathTraversalError(f"path has leading or trailing whitespace: {path!r}")
    if not path:
        raise PathTraversalError("path is empty")

    candidate = path.replace("\\", "/")
    if candidate.startswith("/"):
        raise PathTraversalError(f"absolute paths are not allowed: {path!r}")
    if len(candidate) > 1 and candidate[1] == ":":
        raise PathTraversalError(f"drive-qualified paths are not allowed: {path!r}")

    segments = [seg for seg in candidate.split("/") if seg not in ("", ".")]
    if not segments:
        raise PathTraversalError(f"path resolves to nothing: {path!r}")
    if any(seg == ".." for seg in segments):
        raise PathTraversalError(f"path escapes the repository root: {path!r}")

    normalized = posixpath.normpath("/".join(segments))
    # normpath cannot re-introduce ".." here (there are none left), but assert
    # the invariant rather than trusting it.
    if normalized.startswith("..") or normalized.startswith("/"):
        raise PathTraversalError(f"path escapes the repository root: {path!r}")

    lowered = normalized.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if lowered == prefix.rstrip("/") or lowered.startswith(prefix):
            raise PathTraversalError(f"writes under {prefix!r} are refused: {path!r}")
    return normalized


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    """One proposed file write.

    ``sha`` is the *blob* SHA of the file being replaced. It is only needed by
    the Contents API (:func:`write_file`); the trees API in :func:`push_commit`
    derives everything from the base tree and ignores it.
    """

    path: str
    content: str
    sha: Optional[str] = None

    def __post_init__(self) -> None:
        self.path = normalize_repo_path(self.path)
        if not isinstance(self.content, str):
            raise TypeError("FileChange.content must be str")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (content length only, not the content)."""
        return {"path": self.path, "bytes": len(self.content.encode("utf-8"))}


@dataclass
class FileContent:
    """A file read for editing: its text plus the blob SHA needed to update it."""

    path: str
    text: Optional[str]
    sha: Optional[str]
    ref: Optional[str]
    exists: bool
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "path": self.path,
            "text": self.text,
            "sha": self.sha,
            "ref": self.ref,
            "exists": self.exists,
            "truncated": self.truncated,
        }


@dataclass
class BranchRef:
    """The fix branch (REQ-3.1).

    ``created`` is ``False`` when the branch already existed — see
    :func:`create_branch` for why that is a success, not an error.
    """

    name: str
    ref: str
    sha: str
    base_branch: str
    base_sha: str
    created: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "name": self.name,
            "ref": self.ref,
            "sha": self.sha,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "created": self.created,
        }


@dataclass
class CommitResult:
    """A commit that landed on the fix branch (REQ-3.4).

    ``api`` records which endpoint produced it (``"contents"`` or
    ``"git-trees"``), because the two have different atomicity guarantees and
    the distinction matters when reading a failure trace.
    """

    branch: str
    commit_sha: str
    files: list[str] = field(default_factory=list)
    message: str = ""
    html_url: Optional[str] = None
    api: str = "git-trees"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "files": list(self.files),
            "message": self.message,
            "html_url": self.html_url,
            "api": self.api,
        }


@dataclass
class SyntaxCheck:
    """The outcome of validating one changed file (REQ-3.3).

    ``validated`` is the honest bit: it is ``True`` only when we actually parsed
    the file. For a language we have no parser for, ``validated`` is ``False``
    and ``ok`` is ``True`` — we did not find a problem, but we are not claiming
    the file is valid. Callers must not read ``ok`` as "verified".
    """

    path: str
    language: Optional[str]
    validated: bool
    ok: bool
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "path": self.path,
            "language": self.language,
            "validated": self.validated,
            "ok": self.ok,
            "error": self.error,
        }


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

    Mirrors the helper in the read module so the Engineer can fetch the
    repository once and thread it through branch creation, reads, and the push.
    """
    if repo is not None:
        return repo
    gh = resolve_client(client, token)
    return with_backoff(lambda: gh.get_repo(repo_full_name), sleep=sleep)


def _ref_sha(ref_obj: Any) -> Optional[str]:
    """Pull the commit SHA out of a PyGithub ``GitRef``."""
    target = getattr(ref_obj, "object", None)
    return getattr(target, "sha", None) if target is not None else None


# ---------------------------------------------------------------------------
# create_branch (REQ-3.1)
# ---------------------------------------------------------------------------


def fix_branch_name(issue_number: int) -> str:
    """Return the REQ-3.1 branch name for an issue: ``resurrector/fix-issue-{N}``.

    Raises:
        ValueError: when ``issue_number`` is not a positive integer. The branch
            name is interpolated into a git ref, so a bogus value must be
            rejected here rather than producing ``resurrector/fix-issue-None``.
    """
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        raise ValueError(f"issue_number must be an int, got {issue_number!r}")
    if issue_number <= 0:
        raise ValueError(f"issue_number must be positive, got {issue_number}")
    return BRANCH_NAME_TEMPLATE.format(number=issue_number)


def create_branch(
    repo_full_name: str,
    issue_number: int,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    base_branch: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BranchRef:
    """Create ``resurrector/fix-issue-{N}`` from the default branch head (REQ-3.1).

    The base is the repository's ``default_branch`` unless ``base_branch``
    overrides it; its head SHA is read via ``GET /git/ref/heads/{base}`` and
    used as the new ref's starting point, so the fix branch is guaranteed to
    start from the tip of the default branch rather than from whatever the
    Contents API would have picked.

    Already-exists is treated as **success**, not failure: the function returns
    the existing branch with ``created=False``. The Engineer runs from an SQS
    FIFO message that can legitimately be redelivered (visibility-timeout
    expiry, a Lambda retry, a follow-up run for the same issue), and the branch
    name is a pure function of the issue number, so a second run must be able to
    continue rather than dead-ending on a 422. This makes the operation
    idempotent per issue. The trade-off is that a stale branch from an earlier
    failed attempt is reused rather than reset; that is the safer default —
    force-updating someone's ref is destructive and unnecessary, since
    :func:`push_commit` commits on top of whatever the branch head is.

    All API calls go through :func:`with_backoff` (design.md section 10), and
    ``repo`` / ``client`` are injectable so tests need no network.
    """
    name = fix_branch_name(issue_number)
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )

    base = base_branch or getattr(target, "default_branch", None)
    if not base:
        raise ValueError(
            f"could not determine the default branch of {repo_full_name}; "
            "pass base_branch explicitly"
        )

    base_ref = with_backoff(lambda: target.get_git_ref(f"heads/{base}"), sleep=sleep)
    base_sha = _ref_sha(base_ref)
    if not base_sha:
        raise ValueError(f"could not read the head SHA of {base} in {repo_full_name}")

    full_ref = f"refs/heads/{name}"
    try:
        created_ref = with_backoff(
            lambda: target.create_git_ref(ref=full_ref, sha=base_sha), sleep=sleep
        )
    except GithubException as exc:
        if exc.status != _UNPROCESSABLE:
            raise
        LOGGER.info(
            "branch %s already exists in %s; reusing it", name, repo_full_name
        )
        existing = with_backoff(
            lambda: target.get_git_ref(f"heads/{name}"), sleep=sleep
        )
        return BranchRef(
            name=name,
            ref=full_ref,
            sha=_ref_sha(existing) or base_sha,
            base_branch=base,
            base_sha=base_sha,
            created=False,
        )

    return BranchRef(
        name=name,
        ref=full_ref,
        sha=_ref_sha(created_ref) or base_sha,
        base_branch=base,
        base_sha=base_sha,
        created=True,
    )


# ---------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------


def get_file(
    repo_full_name: str,
    path: str,
    *,
    ref: Optional[str] = None,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> FileContent:
    """Read one file's text **and its blob SHA** at ``ref``.

    Distinct from :func:`src.tools.github_tools.get_file_contents`, which
    returns text only: the Engineer needs the blob SHA because ``PUT
    /repos/{owner}/{repo}/contents/{path}`` requires it to update an existing
    file. Returning them together avoids a second round-trip and avoids the
    read-write race of fetching the SHA later.

    A missing file is **not** an error — it returns ``exists=False`` with
    ``sha=None``, which is exactly the input :func:`write_file` needs to create
    it. Oversized files are truncated with the shared
    :data:`~src.tools.github_tools.TRUNCATION_MARKER` and flagged; a truncated
    read must never be used as the base for an edit, and ``truncated`` is how a
    caller knows.
    """
    clean_path = normalize_repo_path(path)
    max_bytes = (
        max_bytes
        if max_bytes is not None
        else _env_int(MAX_FILE_BYTES_ENV_VAR, DEFAULT_MAX_FILE_BYTES)
    )
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )

    def fetch() -> Any:
        if ref is not None:
            return target.get_contents(clean_path, ref=ref)
        return target.get_contents(clean_path)

    try:
        contents = with_backoff(fetch, sleep=sleep)
    except UnknownObjectException:
        return FileContent(clean_path, None, None, ref, exists=False)
    except GithubException as exc:
        if exc.status == 404:
            return FileContent(clean_path, None, None, ref, exists=False)
        raise

    if isinstance(contents, list):
        LOGGER.info("path %s in %s is a directory", clean_path, repo_full_name)
        return FileContent(clean_path, None, None, ref, exists=True)

    sha = getattr(contents, "sha", None)
    try:
        raw = contents.decoded_content
    except Exception:  # noqa: BLE001 - PyGithub raises assorted decode errors
        return FileContent(clean_path, None, sha, ref, exists=True)
    if raw is None:
        return FileContent(clean_path, None, sha, ref, exists=True)

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            return FileContent(clean_path, None, sha, ref, exists=True)
        text = raw.decode("utf-8", errors="ignore")
    if truncated:
        text += TRUNCATION_MARKER.format(limit=max_bytes)
    return FileContent(clean_path, text, sha, ref, exists=True, truncated=truncated)


# ---------------------------------------------------------------------------
# Change-set limits
# ---------------------------------------------------------------------------


def coerce_changes(
    changes: Iterable[Any],
    *,
    max_files: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> list[FileChange]:
    """Validate an arbitrary iterable into a bounded list of :class:`FileChange`.

    Accepts :class:`FileChange` instances or ``{"path": ..., "content": ...}``
    mappings — the latter is the shape that comes back from the model, so this
    is the choke point where untrusted model output becomes a typed change set.
    Every path goes through :func:`normalize_repo_path`.

    Bounds (``RESURRECTOR_MAX_CHANGED_FILES`` / ``RESURRECTOR_MAX_WRITE_BYTES``)
    exist because the model is capable of proposing a thousand-file "fix", and
    a bounded change set is also a reviewable one.

    Raises:
        ValueError: on a duplicate path, an empty change set, or either cap
            being exceeded.
    """
    max_files = (
        max_files
        if max_files is not None
        else _env_int(MAX_CHANGED_FILES_ENV_VAR, DEFAULT_MAX_CHANGED_FILES)
    )
    max_bytes = (
        max_bytes
        if max_bytes is not None
        else _env_int(MAX_WRITE_BYTES_ENV_VAR, DEFAULT_MAX_WRITE_BYTES)
    )

    coerced: list[FileChange] = []
    seen: set[str] = set()
    for item in changes:
        if isinstance(item, FileChange):
            change = item
        elif isinstance(item, dict):
            if "path" not in item or "content" not in item:
                raise ValueError(
                    f"change entry needs 'path' and 'content' keys, got {sorted(item)}"
                )
            change = FileChange(
                path=item["path"],
                content=item["content"],
                sha=item.get("sha"),
            )
        else:
            raise ValueError(f"unsupported change entry: {type(item).__name__}")

        size = len(change.content.encode("utf-8"))
        if size > max_bytes:
            raise ValueError(
                f"{change.path} is {size} bytes, over the {max_bytes}-byte limit"
            )
        if change.path in seen:
            raise ValueError(f"duplicate path in change set: {change.path}")
        seen.add(change.path)
        coerced.append(change)

    if not coerced:
        raise ValueError("change set is empty")
    if len(coerced) > max_files:
        raise ValueError(
            f"{len(coerced)} files changed, over the {max_files}-file limit"
        )
    return coerced


# ---------------------------------------------------------------------------
# Syntax validation (REQ-3.3)
# ---------------------------------------------------------------------------

_PYTHON_SUFFIXES = (".py", ".pyi")
_JSON_SUFFIXES = (".json",)


def validate_syntax(path: str, content: str) -> SyntaxCheck:
    """Check whether one changed file is syntactically valid (REQ-3.3).

    Real validation, honestly scoped:

    - ``.py`` / ``.pyi`` — parsed with :func:`ast.parse`. This is the actual
      CPython grammar, so a ``SyntaxError`` here is the same one the interpreter
      would raise at import time.
    - ``.json`` — parsed with :func:`json.loads`.
    - everything else — **not validated**. There is no bundled parser for
      JavaScript, Go, Rust, or YAML, and shelling out to a language toolchain
      that may not exist in the Lambda image would be a fake check. The returned
      :class:`SyntaxCheck` says ``validated=False`` so nothing downstream can
      mistake "we did not look" for "we looked and it was fine". For those
      languages the repo's own test suite (REQ-3.2) is the real gate, which is
      one more reason the Engineer prefers repos where tests exist.
    """
    clean = normalize_repo_path(path)
    lowered = clean.lower()

    if lowered.endswith(_PYTHON_SUFFIXES):
        try:
            ast.parse(content, filename=clean)
        except SyntaxError as exc:
            return SyntaxCheck(
                path=clean,
                language="python",
                validated=True,
                ok=False,
                error=f"{exc.__class__.__name__}: {exc.msg} (line {exc.lineno})",
            )
        except ValueError as exc:  # e.g. a NUL byte in the source
            return SyntaxCheck(
                path=clean,
                language="python",
                validated=True,
                ok=False,
                error=f"ValueError: {exc}",
            )
        return SyntaxCheck(clean, "python", validated=True, ok=True)

    if lowered.endswith(_JSON_SUFFIXES):
        try:
            json.loads(content)
        except ValueError as exc:
            return SyntaxCheck(
                path=clean,
                language="json",
                validated=True,
                ok=False,
                error=f"JSONDecodeError: {exc}",
            )
        return SyntaxCheck(clean, "json", validated=True, ok=True)

    return SyntaxCheck(clean, None, validated=False, ok=True, error=None)


def validate_changes(changes: Sequence[FileChange]) -> list[SyntaxCheck]:
    """Run :func:`validate_syntax` over a whole change set."""
    return [validate_syntax(change.path, change.content) for change in changes]


def syntax_failures(checks: Sequence[SyntaxCheck]) -> list[SyntaxCheck]:
    """Return only the checks that actually failed."""
    return [check for check in checks if check.validated and not check.ok]


# ---------------------------------------------------------------------------
# write_file (REQ-3.4, single file, Contents API)
# ---------------------------------------------------------------------------


def write_file(
    repo_full_name: str,
    path: str,
    content: str,
    *,
    branch: str,
    message: str,
    sha: Optional[str] = None,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CommitResult:
    """Create or update one file on ``branch`` via the Contents API (REQ-3.4).

    Dispatch: a ``sha`` means "update this blob"
    (``repo.update_file``); no ``sha`` means "create this file"
    (``repo.create_file``). When a create comes back 422 — GitHub's answer when
    the file already exists and the request omitted its SHA — we fetch the
    current blob SHA and retry as an update. That recovery exists because the
    model can easily believe it is creating a file that is already there, and
    the alternative is a hard failure on a change that is perfectly valid.

    ``branch`` is required and always passed through: the Contents API defaults
    to the **default branch** when it is omitted, which would commit a
    half-validated fix straight to ``main``. Not a default we want anywhere near
    this code path.

    Returns the resulting :class:`CommitResult` (``api="contents"``, one file).
    """
    clean_path = normalize_repo_path(path)
    if not isinstance(content, str):
        raise TypeError("content must be str")
    if not branch:
        raise ValueError("branch is required; refusing to write to the default branch")
    max_bytes = (
        max_bytes
        if max_bytes is not None
        else _env_int(MAX_WRITE_BYTES_ENV_VAR, DEFAULT_MAX_WRITE_BYTES)
    )
    size = len(content.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"{clean_path} is {size} bytes, over the {max_bytes}-byte limit")

    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )

    def _update(blob_sha: str) -> Any:
        return with_backoff(
            lambda: target.update_file(
                clean_path, message, content, blob_sha, branch=branch
            ),
            sleep=sleep,
        )

    if sha:
        response = _update(sha)
    else:
        try:
            response = with_backoff(
                lambda: target.create_file(clean_path, message, content, branch=branch),
                sleep=sleep,
            )
        except GithubException as exc:
            if exc.status != _UNPROCESSABLE:
                raise
            LOGGER.info(
                "%s already exists on %s; retrying as an update", clean_path, branch
            )
            existing = get_file(
                repo_full_name,
                clean_path,
                ref=branch,
                repo=target,
                sleep=sleep,
            )
            if not existing.sha:
                raise
            response = _update(existing.sha)

    commit = response.get("commit") if isinstance(response, dict) else None
    return CommitResult(
        branch=branch,
        commit_sha=getattr(commit, "sha", "") or "",
        files=[clean_path],
        message=message,
        html_url=getattr(commit, "html_url", None),
        api="contents",
    )


# ---------------------------------------------------------------------------
# push_commit (REQ-3.4, atomic multi-file, git trees API)
# ---------------------------------------------------------------------------


def push_commit(
    repo_full_name: str,
    changes: Sequence[Any],
    *,
    branch: str,
    message: str,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    max_files: Optional[int] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CommitResult:
    """Push all of ``changes`` to ``branch`` as **one** commit (REQ-3.4).

    Uses the lower-level git data API rather than the Contents API:

    1. ``GET /git/ref/heads/{branch}`` → the current head SHA (the parent)
    2. ``GET /git/commits/{sha}`` → that commit, for its tree
    3. ``POST /git/trees`` with ``base_tree`` → a new tree carrying every blob
    4. ``POST /git/commits`` → one commit with the new tree and the old parent
    5. ``PATCH /git/refs/heads/{branch}`` → fast-forward the branch to it

    Why the trees API and not one Contents-API call per file
    -------------------------------------------------------
    The Contents API commits exactly one file per request, so an N-file fix
    becomes N commits. Three problems with that here:

    - **Atomicity.** If call 2 of 3 fails (rate limit, timeout, a 409 from a
      concurrent update) the branch is left holding a partial fix that does not
      compile. REQ-3.3 says an invalid fix must abort *without* opening a PR;
      leaving a broken branch behind and calling it an abort is a weaker
      promise than we can make. The trees API moves the ref exactly once, so
      the branch goes from "no fix" to "whole fix" with nothing in between.
    - **Truth in labelling.** REQ-3.2 requires the tests to have passed for the
      fix. Tests are run against the complete change set, so the thing we
      validated is a *tree*, not a sequence of files. One commit per tree keeps
      the claim and the artifact aligned.
    - **Reviewability.** A maintainer reading a PR from an unfamiliar bot should
      see one coherent commit, not "fix part 1/4".

    Cost is the same order either way (5 calls total, versus 1–2 per file), and
    it drops below the Contents API as soon as a fix touches three files.

    Trade-off accepted: the trees API needs the parent commit and base tree, so
    it cannot be used to write to a branch that does not exist yet. That is
    fine — :func:`create_branch` always runs first (REQ-3.1).
    """
    if not branch:
        raise ValueError("branch is required")
    if not message or not message.strip():
        raise ValueError("a commit message is required")

    coerced = coerce_changes(changes, max_files=max_files, max_bytes=max_bytes)
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )

    head_ref = with_backoff(lambda: target.get_git_ref(f"heads/{branch}"), sleep=sleep)
    parent_sha = _ref_sha(head_ref)
    if not parent_sha:
        raise ValueError(f"could not read the head SHA of {branch} in {repo_full_name}")

    parent_commit = with_backoff(
        lambda: target.get_git_commit(parent_sha), sleep=sleep
    )
    base_tree = getattr(parent_commit, "tree", None)
    if base_tree is None:
        raise ValueError(f"could not read the base tree of {parent_sha}")

    elements = [
        InputGitTreeElement(
            path=change.path, mode=BLOB_MODE, type="blob", content=change.content
        )
        for change in coerced
    ]
    new_tree = with_backoff(
        lambda: target.create_git_tree(elements, base_tree), sleep=sleep
    )
    commit = with_backoff(
        lambda: target.create_git_commit(message, new_tree, [parent_commit]),
        sleep=sleep,
    )
    commit_sha = getattr(commit, "sha", "") or ""
    with_backoff(lambda: head_ref.edit(commit_sha), sleep=sleep)

    return CommitResult(
        branch=branch,
        commit_sha=commit_sha,
        files=[change.path for change in coerced],
        message=message,
        html_url=getattr(commit, "html_url", None),
        api="git-trees",
    )
