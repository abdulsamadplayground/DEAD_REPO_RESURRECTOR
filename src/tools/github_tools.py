"""Read-only GitHub reads backing the Analyst sub-agent.

The Analyst is **strictly read-only** against GitHub (design.md section 3). This
module holds the plain, framework-free functions behind the Analyst's read
tools:

- :func:`get_repo_issues` — open issues sorted by 👍 reactions (REQ-2.1)
- :func:`get_file_contents` — a single file's text, size-capped
- :func:`get_repo_structure` — the file tree, depth- and count-capped
- :func:`get_readme`, :func:`get_primary_language`, :func:`get_recent_commits`
- :func:`gather_repo_context` — all of REQ-2.1's reads in one call

Deliberately **no** ``strands`` import lives here. The ``@tool``-decorated
adapters live in :mod:`src.agents.analyst`; everything in this module is a plain
function so it stays importable and testable without the agent framework
installed. Nothing here mutates GitHub state — there is no code path that
creates, edits, or deletes anything.

Design references
-----------------
- Section 6: reads go through ``PyGithub`` (issues, README, tree, commits,
  reactions); auth is a fine-grained PAT from Secrets Manager, cached per
  container.
- Section 10: GitHub rate-limit errors trigger exponential backoff. We reuse
  :func:`src.tools.github_search.with_backoff` rather than writing a second
  implementation.

Context-budget caps
-------------------
Every read is bounded so a pathological repo can never blow up the model's
context window or the API rate-limit budget:

- ``DEFAULT_ISSUE_LIMIT`` (10) issues returned, drawn from at most
  ``DEFAULT_ISSUE_SCAN_LIMIT`` (100) scanned open issues.
- ``DEFAULT_MAX_FILE_BYTES`` (64 KiB) per file, truncated with an explicit
  marker rather than silently cut.
- ``DEFAULT_MAX_TREE_ENTRIES`` (300) tree entries at ``DEFAULT_MAX_TREE_DEPTH``
  (3) levels of nesting.
- ``DEFAULT_COMMIT_LIMIT`` (10) commits, matching REQ-2.1's "last 10 commits".

Configuration (env-var defaults, all overridable per call):

- ``RESURRECTOR_ISSUE_LIMIT`` — issues returned per repo (default 10)
- ``RESURRECTOR_ISSUE_SCAN_LIMIT`` — open issues inspected per repo (default 100)
- ``RESURRECTOR_MAX_FILE_BYTES`` — per-file byte cap (default 65536)
- ``RESURRECTOR_MAX_TREE_ENTRIES`` — tree entry cap (default 300)
- ``RESURRECTOR_MAX_TREE_DEPTH`` — tree depth cap (default 3)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from github import Auth, Github
from github.GithubException import GithubException, UnknownObjectException

from src.tools.github_search import with_backoff

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ISSUE_LIMIT = 10
DEFAULT_ISSUE_SCAN_LIMIT = 100
DEFAULT_MAX_FILE_BYTES = 64 * 1024
DEFAULT_MAX_TREE_ENTRIES = 300
DEFAULT_MAX_TREE_DEPTH = 3
#: REQ-2.1 asks for "the last 10 commits" — this is the requirement, not a knob.
DEFAULT_COMMIT_LIMIT = 10

ISSUE_LIMIT_ENV_VAR = "RESURRECTOR_ISSUE_LIMIT"
ISSUE_SCAN_LIMIT_ENV_VAR = "RESURRECTOR_ISSUE_SCAN_LIMIT"
MAX_FILE_BYTES_ENV_VAR = "RESURRECTOR_MAX_FILE_BYTES"
MAX_TREE_ENTRIES_ENV_VAR = "RESURRECTOR_MAX_TREE_ENTRIES"
MAX_TREE_DEPTH_ENV_VAR = "RESURRECTOR_MAX_TREE_DEPTH"

GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"

#: Appended to a file body that hit the byte cap, so the model (and any human
#: reading the trace) can tell the content is incomplete.
TRUNCATION_MARKER = "\n...[truncated by resurrector: file exceeds {limit} bytes]"


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid.

    Mirrors the ``_env_*`` convention already used in
    :mod:`src.tools.github_search` and :mod:`src.lambdas.scanner_lambda`.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to timezone-aware UTC (naive values assumed UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 render a datetime, or ``None``."""
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def resolve_client(
    client: Optional[Github] = None, token: Optional[str] = None
) -> Github:
    """Return an injected client, or build one from a token / configured secret.

    Resolution order:

    1. an explicitly injected ``client`` (how every test supplies its fake),
    2. an explicit ``token``,
    3. the ``GITHUB_TOKEN`` env var (local / SAM-local testing),
    4. the Secrets-Manager-backed loader already implemented for the Scanner,
       :func:`src.lambdas.scanner_lambda.load_github_token`, which caches the
       PAT per container (design.md section 6).

    Step 4 is imported **lazily**, inside this function. A top-level import
    would make ``src.tools`` depend on ``src.lambdas`` (backwards layering) and
    would drag boto3 client construction into every import of this module. The
    lazy import keeps the dependency at call time, only on the path that
    actually needs Secrets Manager, and avoids duplicating a second token
    loader.
    """
    if client is not None:
        return client
    if token is not None:
        return Github(auth=Auth.Token(token))
    env_token = os.environ.get(GITHUB_TOKEN_ENV_VAR)
    if env_token:
        return Github(auth=Auth.Token(env_token))
    try:
        from src.lambdas.scanner_lambda import load_github_token
    except ImportError:  # pragma: no cover - boto3 is a hard dependency
        raise ValueError("a Github client or token is required")
    return Github(auth=Auth.Token(load_github_token()))


def _get_repo(
    repo_full_name: str,
    *,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch the PyGithub ``Repository`` for ``repo_full_name`` with backoff."""
    gh = resolve_client(client, token)
    return with_backoff(lambda: gh.get_repo(repo_full_name), sleep=sleep)


def _resolve_repo(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Return an already-fetched ``repo`` or look one up.

    Lets :func:`gather_repo_context` fetch the repository once and share it
    across all five reads instead of paying ``GET /repos/{owner}/{repo}`` per
    tool call.
    """
    if repo is not None:
        return repo
    return _get_repo(repo_full_name, client=client, token=token, sleep=sleep)


# ---------------------------------------------------------------------------
# Data model — lightweight, JSON-friendly views of PyGithub objects
# ---------------------------------------------------------------------------


@dataclass
class IssueSummary:
    """A network-free view of one open issue.

    Returned instead of a raw PyGithub ``Issue`` so downstream code (the
    complexity scorer, the agent prompt builder, the tests) never triggers a
    lazy API round-trip by touching an attribute.
    """

    number: int
    title: str
    body: Optional[str]
    thumbs_up: int
    total_reactions: int
    comments: int
    labels: list[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    html_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "thumbs_up": self.thumbs_up,
            "total_reactions": self.total_reactions,
            "comments": self.comments,
            "labels": list(self.labels),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "html_url": self.html_url,
        }


@dataclass
class CommitSummary:
    """A network-free view of one commit."""

    sha: str
    message: str
    author: Optional[str]
    committed_at: Optional[str]
    html_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "sha": self.sha,
            "message": self.message,
            "author": self.author,
            "committed_at": self.committed_at,
            "html_url": self.html_url,
        }


@dataclass
class TreeEntry:
    """One entry in a repo's file tree."""

    path: str
    type: str
    size: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {"path": self.path, "type": self.type, "size": self.size}


@dataclass
class RepoStructure:
    """A depth- and count-capped view of a repo's file tree.

    ``truncated`` is ``True`` when entries were dropped by either cap, so the
    Analyst prompt can say "partial tree" instead of implying it saw everything.
    """

    entries: list[TreeEntry] = field(default_factory=list)
    truncated: bool = False
    max_depth: int = DEFAULT_MAX_TREE_DEPTH
    max_entries: int = DEFAULT_MAX_TREE_ENTRIES

    def paths(self) -> list[str]:
        """Just the paths, in tree order."""
        return [entry.path for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "truncated": self.truncated,
            "max_depth": self.max_depth,
            "max_entries": self.max_entries,
        }


@dataclass
class RepoContext:
    """Everything REQ-2.1 requires the Analyst to read, in one object.

    REQ-2.1: "the open issues sorted by 👍 reactions, the README, primary
    language, folder structure, and the last 10 commits" — one field each, so
    coverage of the requirement is explicit and assertable.
    """

    repo_full_name: str
    default_branch: Optional[str] = None
    primary_language: Optional[str] = None
    readme: Optional[str] = None
    structure: Optional[RepoStructure] = None
    issues: list[IssueSummary] = field(default_factory=list)
    recent_commits: list[CommitSummary] = field(default_factory=list)

    def top_issue(self) -> Optional[IssueSummary]:
        """The top-voted candidate issue (REQ-2.2), or ``None`` if there are none."""
        return self.issues[0] if self.issues else None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "repo_full_name": self.repo_full_name,
            "default_branch": self.default_branch,
            "primary_language": self.primary_language,
            "readme": self.readme,
            "structure": self.structure.to_dict() if self.structure else None,
            "issues": [issue.to_dict() for issue in self.issues],
            "recent_commits": [commit.to_dict() for commit in self.recent_commits],
        }


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def _reaction_counts(issue: Any) -> tuple[int, int]:
    """Return ``(thumbs_up, total_reactions)`` for an issue.

    PyGithub exposes a ``reactions`` summary dict (``{"+1": 3, "total_count":
    5, ...}``) that ships inline with the issue payload, so the common path
    costs **zero** extra API calls. Only when that summary is absent do we fall
    back to paging ``get_reactions()``, which is one extra request per issue —
    an N+1 pattern we deliberately avoid unless forced. Anything unreadable
    scores 0 rather than raising; a missing reaction count must not abort an
    analysis.
    """
    reactions = getattr(issue, "reactions", None)
    if isinstance(reactions, dict):
        thumbs_up = int(reactions.get("+1") or 0)
        total = int(reactions.get("total_count") or 0)
        return thumbs_up, total

    getter = getattr(issue, "get_reactions", None)
    if getter is None:
        return 0, 0
    try:
        contents = [getattr(r, "content", None) for r in getter()]
    except GithubException:
        LOGGER.warning("could not read reactions for issue %s", getattr(issue, "number", "?"))
        return 0, 0
    return sum(1 for c in contents if c == "+1"), len(contents)


def _is_pull_request(issue: Any) -> bool:
    """True when a REST "issue" is really a pull request.

    GitHub models PRs as issues, so ``get_issues`` returns both. REQ-2.1 is
    about *issues*, and the Analyst is read-only, so PRs are filtered out.
    """
    return getattr(issue, "pull_request", None) is not None


def _issue_summary(issue: Any) -> IssueSummary:
    """Convert a PyGithub ``Issue`` into an :class:`IssueSummary`."""
    thumbs_up, total = _reaction_counts(issue)
    labels = []
    for label in getattr(issue, "labels", None) or []:
        name = getattr(label, "name", None)
        labels.append(name if name is not None else str(label))
    return IssueSummary(
        number=int(issue.number),
        title=getattr(issue, "title", "") or "",
        body=getattr(issue, "body", None),
        thumbs_up=thumbs_up,
        total_reactions=total,
        comments=int(getattr(issue, "comments", 0) or 0),
        labels=labels,
        created_at=_iso(getattr(issue, "created_at", None)),
        updated_at=_iso(getattr(issue, "updated_at", None)),
        html_url=getattr(issue, "html_url", None),
    )


def _issue_sort_key(issue: IssueSummary) -> tuple[int, int, int, int]:
    """Deterministic ranking key for candidate issues.

    REQ-2.1 only specifies "sorted by 👍 reactions", which leaves ties
    undefined. Ties are common on abandoned repos (a long tail of issues with
    zero reactions), and an unstable order would make the Analyst pick a
    different issue on every run for the same repo. The tie-break chain,
    strongest signal first:

    1. 👍 (``+1``) count, descending — the requirement.
    2. total reaction count, descending — any engagement beats none.
    3. comment count, descending — discussion implies the issue still matters.
    4. issue number, **ascending** — oldest first, fully deterministic.

    Returned as a tuple of negated values so a plain ascending ``sorted`` gives
    the intended order.
    """
    return (-issue.thumbs_up, -issue.total_reactions, -issue.comments, issue.number)


# ---------------------------------------------------------------------------
# get_repo_issues (REQ-2.1)
# ---------------------------------------------------------------------------


def get_repo_issues(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    limit: Optional[int] = None,
    scan_limit: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[IssueSummary]:
    """Return this repo's open issues, most 👍-reacted first (REQ-2.1).

    Pull requests are filtered out (GitHub returns them from the issues
    endpoint). At most ``scan_limit`` open issues are inspected and at most
    ``limit`` are returned; both default from env vars. Ranking and
    tie-breaking are documented on :func:`_issue_sort_key`.

    All API access goes through :func:`with_backoff`, and ``repo`` / ``client``
    are injectable so tests need no network.
    """
    limit = limit if limit is not None else _env_int(ISSUE_LIMIT_ENV_VAR, DEFAULT_ISSUE_LIMIT)
    scan_limit = (
        scan_limit
        if scan_limit is not None
        else _env_int(ISSUE_SCAN_LIMIT_ENV_VAR, DEFAULT_ISSUE_SCAN_LIMIT)
    )

    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    raw_issues = with_backoff(lambda: target.get_issues(state="open"), sleep=sleep)

    summaries: list[IssueSummary] = []
    scanned = 0
    for issue in raw_issues:
        if scanned >= scan_limit:
            break
        scanned += 1
        if _is_pull_request(issue):
            continue
        summaries.append(_issue_summary(issue))

    summaries.sort(key=_issue_sort_key)
    return summaries[:limit] if limit >= 0 else summaries


# ---------------------------------------------------------------------------
# get_file_contents
# ---------------------------------------------------------------------------


def get_file_contents(
    repo_full_name: str,
    path: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    ref: Optional[str] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[str]:
    """Return the decoded text of one file, or ``None`` when unreadable.

    ``None`` means "there is no text here for the model to reason about" and
    covers every degenerate case: the path is missing, the path is a directory,
    or the bytes are not valid UTF-8 (a binary file). Callers therefore only
    have one failure shape to handle.

    Oversized files are **truncated rather than dropped** — the first
    ``max_bytes`` bytes are returned with :data:`TRUNCATION_MARKER` appended, so
    the model gets useful context and can still tell the content is incomplete.
    """
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
            return target.get_contents(path, ref=ref)
        return target.get_contents(path)

    try:
        contents = with_backoff(fetch, sleep=sleep)
    except UnknownObjectException:
        LOGGER.info("file %s not found in %s", path, repo_full_name)
        return None
    except GithubException as exc:
        if exc.status == 404:
            return None
        raise

    if isinstance(contents, list):
        # ``path`` is a directory; use get_repo_structure for that.
        LOGGER.info("path %s in %s is a directory, not a file", path, repo_full_name)
        return None

    try:
        raw = contents.decoded_content
    except Exception:  # noqa: BLE001 - PyGithub raises assorted decode errors
        LOGGER.info("could not decode %s in %s", path, repo_full_name)
        return None
    if raw is None:
        return None

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            LOGGER.info("file %s in %s is not UTF-8 text", path, repo_full_name)
            return None
        # A hard cut can land mid-codepoint; drop the partial tail instead of
        # declaring an otherwise-textual file binary.
        text = raw.decode("utf-8", errors="ignore")
        if not text.strip():
            return None

    if truncated:
        text += TRUNCATION_MARKER.format(limit=max_bytes)
    return text


# ---------------------------------------------------------------------------
# get_repo_structure
# ---------------------------------------------------------------------------


def get_repo_structure(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    ref: Optional[str] = None,
    max_entries: Optional[int] = None,
    max_depth: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RepoStructure:
    """Return the repo's file tree, capped by depth and entry count.

    Uses one recursive git-tree call. ``max_depth`` counts path segments, so
    depth 1 keeps top-level entries only, depth 2 also keeps ``src/foo.py``, and
    so on. ``max_entries`` bounds the total. Hitting either cap sets
    :attr:`RepoStructure.truncated`.

    Both caps exist to protect the model context window: some abandoned repos
    carry vendored dependency trees with tens of thousands of files.
    """
    max_entries = (
        max_entries
        if max_entries is not None
        else _env_int(MAX_TREE_ENTRIES_ENV_VAR, DEFAULT_MAX_TREE_ENTRIES)
    )
    max_depth = (
        max_depth
        if max_depth is not None
        else _env_int(MAX_TREE_DEPTH_ENV_VAR, DEFAULT_MAX_TREE_DEPTH)
    )

    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    tree_ref = ref or getattr(target, "default_branch", None) or "HEAD"
    tree = with_backoff(
        lambda: target.get_git_tree(tree_ref, recursive=True), sleep=sleep
    )

    entries: list[TreeEntry] = []
    truncated = bool(getattr(tree, "truncated", False))
    for element in getattr(tree, "tree", None) or []:
        path = getattr(element, "path", None)
        if not path:
            continue
        if path.count("/") + 1 > max_depth:
            truncated = True
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        entries.append(
            TreeEntry(
                path=path,
                type=getattr(element, "type", "blob") or "blob",
                size=getattr(element, "size", None),
            )
        )

    return RepoStructure(
        entries=entries,
        truncated=truncated,
        max_depth=max_depth,
        max_entries=max_entries,
    )


# ---------------------------------------------------------------------------
# README / language / commits (the rest of REQ-2.1)
# ---------------------------------------------------------------------------


def get_readme(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    ref: Optional[str] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[str]:
    """Return the repo's README text, or ``None`` when there isn't one.

    Uses ``get_readme()`` so GitHub resolves the filename/extension for us
    (``README``, ``README.md``, ``README.rst``, ...). Same size cap and
    truncation marker as :func:`get_file_contents`.
    """
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
            return target.get_readme(ref=ref)
        return target.get_readme()

    try:
        readme = with_backoff(fetch, sleep=sleep)
    except UnknownObjectException:
        LOGGER.info("%s has no README", repo_full_name)
        return None
    except GithubException as exc:
        if exc.status == 404:
            return None
        raise

    try:
        raw = readme.decoded_content
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += TRUNCATION_MARKER.format(limit=max_bytes)
    return text


def get_primary_language(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[str]:
    """Return the repo's primary language, or ``None`` when GitHub reports none."""
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    return getattr(target, "language", None)


def get_recent_commits(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    limit: int = DEFAULT_COMMIT_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CommitSummary]:
    """Return the most recent commits, newest first, capped at ``limit``.

    ``limit`` defaults to 10 to match REQ-2.1 ("the last 10 commits"). The
    paginated list is only walked as far as the cap, so we never pay for pages
    we would discard.
    """
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )
    raw_commits = with_backoff(lambda: target.get_commits(), sleep=sleep)

    commits: list[CommitSummary] = []
    for commit in raw_commits:
        if len(commits) >= limit:
            break
        git_commit = getattr(commit, "commit", None)
        message = getattr(git_commit, "message", "") or ""
        author_name = None
        git_author = getattr(git_commit, "author", None)
        if git_author is not None:
            author_name = getattr(git_author, "name", None)
        if author_name is None:
            top_author = getattr(commit, "author", None)
            author_name = getattr(top_author, "login", None)
        committed_at = None
        if git_author is not None:
            committed_at = _iso(getattr(git_author, "date", None))
        commits.append(
            CommitSummary(
                sha=getattr(commit, "sha", "") or "",
                message=message,
                author=author_name,
                committed_at=committed_at,
                html_url=getattr(commit, "html_url", None),
            )
        )
    return commits


# ---------------------------------------------------------------------------
# gather_repo_context — all of REQ-2.1 in one read
# ---------------------------------------------------------------------------


def gather_repo_context(
    repo_full_name: str,
    *,
    repo: Any = None,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    ref: Optional[str] = None,
    issue_limit: Optional[int] = None,
    issue_scan_limit: Optional[int] = None,
    commit_limit: int = DEFAULT_COMMIT_LIMIT,
    max_entries: Optional[int] = None,
    max_depth: Optional[int] = None,
    max_bytes: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RepoContext:
    """Read everything REQ-2.1 requires, in one pass over one repo handle.

    Returns a :class:`RepoContext` carrying: open issues sorted by 👍, the
    README, the primary language, the folder structure, and the last 10
    commits.

    The repository object is fetched once and reused, so the whole context costs
    one repo lookup plus one call per read rather than five repo lookups. A
    failure in any single read is logged and degraded to ``None`` / an empty
    list: a missing README must not cost us the issue analysis. The issues read
    is the exception — it is the one input REQ-2.2 cannot work without, so its
    errors propagate.
    """
    target = _resolve_repo(
        repo_full_name, repo=repo, client=client, token=token, sleep=sleep
    )

    issues = get_repo_issues(
        repo_full_name,
        repo=target,
        limit=issue_limit,
        scan_limit=issue_scan_limit,
        sleep=sleep,
    )

    def _safe(label: str, thunk: Callable[[], Any], default: Any) -> Any:
        try:
            return thunk()
        except Exception:  # noqa: BLE001 - one soft read must not fail the analysis
            LOGGER.warning("could not read %s for %s", label, repo_full_name, exc_info=True)
            return default

    readme = _safe(
        "readme",
        lambda: get_readme(
            repo_full_name, repo=target, ref=ref, max_bytes=max_bytes, sleep=sleep
        ),
        None,
    )
    structure = _safe(
        "structure",
        lambda: get_repo_structure(
            repo_full_name,
            repo=target,
            ref=ref,
            max_entries=max_entries,
            max_depth=max_depth,
            sleep=sleep,
        ),
        None,
    )
    commits = _safe(
        "commits",
        lambda: get_recent_commits(
            repo_full_name, repo=target, limit=commit_limit, sleep=sleep
        ),
        [],
    )

    return RepoContext(
        repo_full_name=repo_full_name,
        default_branch=getattr(target, "default_branch", None),
        primary_language=getattr(target, "language", None),
        readme=readme,
        structure=structure,
        issues=issues,
        recent_commits=commits,
    )
