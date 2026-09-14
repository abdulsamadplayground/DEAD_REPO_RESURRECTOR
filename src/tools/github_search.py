"""GitHub Search wrapper for discovering likely-abandoned repositories.

The Scanner Lambda (Task 4) uses this module to run the *distress-pattern*
search every 6 hours (REQ-1.1) and to filter the raw candidates down to repos
worth engaging (REQ-1.2). This module is read-only against GitHub and returns
lightweight :class:`RepoCandidate` records so downstream code never has to
touch raw PyGithub objects.

Distress pattern (see design.md glossary): ``stars:10..500 pushed:<{date}``
where ``{date}`` is the last-commit-age threshold (default 8 months ago),
optionally narrowed by language. Results are then filtered defensively to keep
only repos with ``open_issues >= 20`` and a last commit older than the age
threshold, capped at 20 per run. Note that GitHub's ``open_issues_count``
includes open pull requests, so that gate is approximate — see
:meth:`RepoCandidate.from_repo`.

Design references:
- Section 6 (GitHub integration): read access uses ``PyGithub``; auth is a
  fine-grained PAT loaded from Secrets Manager at cold start.
- Section 10 (Error handling): GitHub rate-limit errors trigger exponential
  backoff in the github tools layer.

Configuration (all overridable via params, with env-var defaults):

- ``RESURRECTOR_SEARCH_MIN_STARS`` / ``RESURRECTOR_SEARCH_MAX_STARS`` — star range (default 10..500)
- ``RESURRECTOR_SEARCH_AGE_MONTHS`` — last-commit-age threshold in months (default 8)
- ``RESURRECTOR_SEARCH_MIN_OPEN_ISSUES`` — open-issue floor (default 20)
- ``RESURRECTOR_SEARCH_MAX_RESULTS`` — cap on returned candidates (default 20)
- ``RESURRECTOR_SEARCH_LANGUAGE`` — optional language filter (default unset)
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from github import Auth, Github
from github.GithubException import GithubException, RateLimitExceededException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MIN_STARS = 10
DEFAULT_MAX_STARS = 500
DEFAULT_AGE_MONTHS = 8
DEFAULT_MIN_OPEN_ISSUES = 20
DEFAULT_MAX_RESULTS = 20

MIN_STARS_ENV_VAR = "RESURRECTOR_SEARCH_MIN_STARS"
MAX_STARS_ENV_VAR = "RESURRECTOR_SEARCH_MAX_STARS"
AGE_MONTHS_ENV_VAR = "RESURRECTOR_SEARCH_AGE_MONTHS"
MIN_OPEN_ISSUES_ENV_VAR = "RESURRECTOR_SEARCH_MIN_OPEN_ISSUES"
MAX_RESULTS_ENV_VAR = "RESURRECTOR_SEARCH_MAX_RESULTS"
LANGUAGE_ENV_VAR = "RESURRECTOR_SEARCH_LANGUAGE"

# Approximate a month as 30 days for the age threshold. The search API only has
# day granularity via the ``pushed:`` qualifier, so this is intentionally
# coarse and matches the "months" wording of REQ-1.2.
_DAYS_PER_MONTH = 30

# Backoff defaults for rate-limit handling.
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 60.0


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now() -> datetime:
    """Current UTC time (indirected so tests can monkeypatch if needed)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RepoCandidate:
    """A lightweight, network-free view of a candidate repository.

    Downstream Scanner code (Task 4) consumes these instead of raw PyGithub
    ``Repository`` objects so it can dedup and enqueue without further API
    calls.
    """

    repo_full_name: str
    stars: int
    open_issues: int
    last_commit_at: Optional[datetime]
    default_branch: str
    language: Optional[str]
    html_url: str

    @classmethod
    def from_repo(cls, repo: Any) -> "RepoCandidate":
        """Build a candidate from a PyGithub ``Repository`` object.

        Reads only already-fetched attributes (search results are hydrated), so
        this does not incur extra API round-trips.

        Fidelity caveat: GitHub's ``open_issues_count`` counts open pull
        requests as well as open issues (PRs are issues in the REST data
        model). ``open_issues`` here inherits that behavior, so the REQ-1.2
        ``open_issues >= 20`` gate is an approximation that can over-count a
        repo with many stale PRs. Getting an exact issue-only count needs a
        separate search (``is:issue is:open repo:...``) per repo, which is not
        worth the extra rate-limit budget at scan time.
        """
        return cls(
            repo_full_name=repo.full_name,
            stars=int(repo.stargazers_count or 0),
            open_issues=int(repo.open_issues_count or 0),
            last_commit_at=_as_utc(getattr(repo, "pushed_at", None)),
            default_branch=repo.default_branch,
            language=getattr(repo, "language", None),
            html_url=repo.html_url,
        )


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to timezone-aware UTC (naive values assumed UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Distress-pattern query builder
# ---------------------------------------------------------------------------


def build_distress_query(
    *,
    min_stars: Optional[int] = None,
    max_stars: Optional[int] = None,
    age_months: Optional[int] = None,
    language: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    """Construct the distress-pattern GitHub search query string.

    Produces ``stars:{min}..{max} pushed:<{YYYY-MM-DD}`` where the date is
    ``age_months`` months before ``now`` (default: 8 months ago). An optional
    ``language:{lang}`` qualifier is appended when a language is configured.

    All parameters fall back to env-var configuration, then to module defaults.
    """
    min_stars = min_stars if min_stars is not None else _env_int(MIN_STARS_ENV_VAR, DEFAULT_MIN_STARS)
    max_stars = max_stars if max_stars is not None else _env_int(MAX_STARS_ENV_VAR, DEFAULT_MAX_STARS)
    age_months = age_months if age_months is not None else _env_int(AGE_MONTHS_ENV_VAR, DEFAULT_AGE_MONTHS)
    if language is None:
        language = os.environ.get(LANGUAGE_ENV_VAR) or None

    reference = now if now is not None else _now()
    threshold_date = reference - timedelta(days=age_months * _DAYS_PER_MONTH)
    date_str = threshold_date.strftime("%Y-%m-%d")

    query = f"stars:{min_stars}..{max_stars} pushed:<{date_str}"
    if language:
        query += f" language:{language}"
    return query


def _age_threshold(age_months: int, now: datetime) -> datetime:
    """Return the cutoff datetime; a repo pushed before this is 'old enough'."""
    return now - timedelta(days=age_months * _DAYS_PER_MONTH)


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


def with_backoff(
    func: Callable[[], Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> Any:
    """Call ``func`` with exponential backoff + jitter on GitHub rate limits.

    Retries specifically on :class:`RateLimitExceededException` and on secondary
    rate limits (a :class:`GithubException` with HTTP 403/429). The delay for
    attempt ``n`` is ``min(max_delay, base_delay * 2**n)`` plus up to one extra
    ``base_delay`` of jitter. Raises the last exception once ``max_retries`` is
    exhausted (design.md section 10).

    ``sleep`` and ``rng`` are injectable so tests run without real delays.
    """
    attempt = 0
    while True:
        try:
            return func()
        except RateLimitExceededException:
            if attempt >= max_retries:
                raise
        except GithubException as exc:  # secondary / abuse rate limits
            if exc.status not in (403, 429) or attempt >= max_retries:
                raise
        delay = min(max_delay, base_delay * (2 ** attempt))
        delay += rng() * base_delay  # jitter
        sleep(delay)
        attempt += 1


# ---------------------------------------------------------------------------
# Search + filter
# ---------------------------------------------------------------------------


def _resolve_client(client: Optional[Github], token: Optional[str]) -> Github:
    """Return an injected client, build one from a token, or from Secrets/env.

    Kept simple and injectable: tests pass a stub ``client``. In production the
    Scanner passes the cold-start-cached PAT-backed client (design.md section 6).
    """
    if client is not None:
        return client
    if token is not None:
        return Github(auth=Auth.Token(token))
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return Github(auth=Auth.Token(env_token))
    raise ValueError("a Github client or token is required")


def search_candidates(
    *,
    client: Optional[Github] = None,
    token: Optional[str] = None,
    min_stars: Optional[int] = None,
    max_stars: Optional[int] = None,
    age_months: Optional[int] = None,
    language: Optional[str] = None,
    min_open_issues: Optional[int] = None,
    max_results: Optional[int] = None,
    now: Optional[datetime] = None,
    sort: str = "updated",
    order: str = "desc",
    sleep: Callable[[float], None] = time.sleep,
) -> list[RepoCandidate]:
    """Run the distress-pattern search and return filtered candidates.

    Implements REQ-1.1 (query the Search API with the distress pattern) and
    REQ-1.2 (filter to ``open_issues >= min_open_issues`` and last commit older
    than the age threshold, capped at ``max_results``).

    The ``pushed:<date`` qualifier already narrows by age; the age filter here
    is a defensive re-check in case the API returns a boundary repo. All API
    calls are wrapped with :func:`with_backoff` for rate-limit resilience.

    Iteration stops as soon as ``max_results`` candidates are collected, so the
    paginated result set is only walked as far as needed — this deliberately
    avoids fetching (and paying rate-limit cost for) pages we would discard.
    """
    age_months = age_months if age_months is not None else _env_int(AGE_MONTHS_ENV_VAR, DEFAULT_AGE_MONTHS)
    min_open_issues = (
        min_open_issues
        if min_open_issues is not None
        else _env_int(MIN_OPEN_ISSUES_ENV_VAR, DEFAULT_MIN_OPEN_ISSUES)
    )
    max_results = (
        max_results if max_results is not None else _env_int(MAX_RESULTS_ENV_VAR, DEFAULT_MAX_RESULTS)
    )
    reference = now if now is not None else _now()

    gh = _resolve_client(client, token)
    query = build_distress_query(
        min_stars=min_stars,
        max_stars=max_stars,
        age_months=age_months,
        language=language,
        now=reference,
    )

    results = with_backoff(
        lambda: gh.search_repositories(query=query, sort=sort, order=order),
        sleep=sleep,
    )

    cutoff = _age_threshold(age_months, reference)
    candidates: list[RepoCandidate] = []
    for repo in results:
        candidate = RepoCandidate.from_repo(repo)
        if candidate.open_issues < min_open_issues:
            continue
        # last_commit_age >= threshold  <=>  last commit older than the cutoff.
        if candidate.last_commit_at is not None and candidate.last_commit_at > cutoff:
            continue
        candidates.append(candidate)
        if len(candidates) >= max_results:
            break

    return candidates
