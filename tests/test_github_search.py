"""Unit tests for src.tools.github_search (GitHub Search wrapper).

All GitHub interaction is mocked — no network access. Fake repo objects mimic
the attributes PyGithub search results expose, and a fake client stands in for
``github.Github`` so we can drive ``search_repositories`` deterministically and
simulate rate-limit exceptions.

Covers:
- distress-pattern query builder (correct string + date threshold)
- filtering by open-issue count and last-commit age
- the 20-result (configurable) cap
- rate-limit backoff/retry with an injected sleep and a client that raises
  RateLimitExceededException once, then succeeds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from github.GithubException import GithubException, RateLimitExceededException

import src.tools.github_search as gs


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeRepo:
    """Mimics the subset of a PyGithub Repository used by RepoCandidate."""

    def __init__(
        self,
        full_name,
        stars=100,
        open_issues=25,
        pushed_at=None,
        default_branch="main",
        language="Python",
    ):
        self.full_name = full_name
        self.stargazers_count = stars
        self.open_issues_count = open_issues
        self.pushed_at = pushed_at
        self.default_branch = default_branch
        self.language = language
        self.html_url = f"https://github.com/{full_name}"


class FakeClient:
    """Stand-in for github.Github; records the query and returns canned repos."""

    def __init__(self, repos):
        self._repos = repos
        self.last_query = None
        self.last_sort = None
        self.last_order = None
        self.call_count = 0

    def search_repositories(self, query, sort=None, order=None):
        self.call_count += 1
        self.last_query = query
        self.last_sort = sort
        self.last_order = order
        return list(self._repos)


NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _old(months):
    """A pushed_at datetime `months` months before NOW (30-day months)."""
    return NOW - timedelta(days=months * 30 + 1)


def _recent(months):
    """A pushed_at datetime `months` months before NOW but still inside window."""
    return NOW - timedelta(days=months * 30 - 1)


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------


def test_build_distress_query_default_pattern_and_date():
    # 8 months * 30 days = 240 days before 2024-06-01 = 2023-10-05
    query = gs.build_distress_query(now=NOW)
    assert query == "stars:10..500 pushed:<2023-10-05"


def test_build_distress_query_custom_range_age_and_language():
    query = gs.build_distress_query(
        min_stars=50,
        max_stars=1000,
        age_months=6,
        language="Rust",
        now=NOW,
    )
    # 6 * 30 = 180 days before 2024-06-01 = 2023-12-04
    assert query == "stars:50..1000 pushed:<2023-12-04 language:Rust"


def test_build_distress_query_reads_env(monkeypatch):
    monkeypatch.setenv(gs.MIN_STARS_ENV_VAR, "5")
    monkeypatch.setenv(gs.MAX_STARS_ENV_VAR, "200")
    monkeypatch.setenv(gs.AGE_MONTHS_ENV_VAR, "12")
    monkeypatch.setenv(gs.LANGUAGE_ENV_VAR, "Go")
    query = gs.build_distress_query(now=NOW)
    # 12 * 30 days before 2024-06-01 = 2023-06-07
    assert query == "stars:5..200 pushed:<2023-06-07 language:Go"


# ---------------------------------------------------------------------------
# RepoCandidate
# ---------------------------------------------------------------------------


def test_repo_candidate_from_repo_maps_fields():
    repo = FakeRepo("owner/repo", stars=42, open_issues=30, pushed_at=_old(9))
    cand = gs.RepoCandidate.from_repo(repo)
    assert cand.repo_full_name == "owner/repo"
    assert cand.stars == 42
    assert cand.open_issues == 30
    assert cand.default_branch == "main"
    assert cand.language == "Python"
    assert cand.html_url == "https://github.com/owner/repo"
    assert cand.last_commit_at == _old(9)


def test_repo_candidate_naive_datetime_becomes_utc():
    naive = datetime(2023, 1, 1)  # no tzinfo
    repo = FakeRepo("owner/repo", pushed_at=naive)
    cand = gs.RepoCandidate.from_repo(repo)
    assert cand.last_commit_at.tzinfo is not None
    assert cand.last_commit_at == naive.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Filtering (REQ-1.2)
# ---------------------------------------------------------------------------


def test_filter_drops_repos_below_min_open_issues():
    repos = [
        FakeRepo("owner/enough", open_issues=25, pushed_at=_old(9)),
        FakeRepo("owner/toofew", open_issues=19, pushed_at=_old(9)),
    ]
    client = FakeClient(repos)
    result = gs.search_candidates(client=client, now=NOW)
    names = [c.repo_full_name for c in result]
    assert names == ["owner/enough"]


def test_filter_drops_repos_pushed_too_recently():
    repos = [
        FakeRepo("owner/old", open_issues=30, pushed_at=_old(9)),
        FakeRepo("owner/recent", open_issues=30, pushed_at=_recent(3)),
    ]
    client = FakeClient(repos)
    result = gs.search_candidates(client=client, now=NOW)
    names = [c.repo_full_name for c in result]
    assert names == ["owner/old"]


def test_filter_keeps_repo_with_missing_pushed_at():
    # Defensive: a repo with no pushed_at is not excluded by the age check.
    repos = [FakeRepo("owner/nopush", open_issues=25, pushed_at=None)]
    client = FakeClient(repos)
    result = gs.search_candidates(client=client, now=NOW)
    assert [c.repo_full_name for c in result] == ["owner/nopush"]


def test_search_passes_query_and_sort_to_client():
    client = FakeClient([])
    gs.search_candidates(client=client, now=NOW, sort="stars", order="asc")
    assert client.last_query == "stars:10..500 pushed:<2023-10-05"
    assert client.last_sort == "stars"
    assert client.last_order == "asc"


# ---------------------------------------------------------------------------
# Result cap (REQ-1.2: at most 20 per run)
# ---------------------------------------------------------------------------


def test_result_cap_defaults_to_20():
    repos = [
        FakeRepo(f"owner/r{i}", open_issues=25, pushed_at=_old(9)) for i in range(50)
    ]
    client = FakeClient(repos)
    result = gs.search_candidates(client=client, now=NOW)
    assert len(result) == 20


def test_result_cap_is_configurable():
    repos = [
        FakeRepo(f"owner/r{i}", open_issues=25, pushed_at=_old(9)) for i in range(10)
    ]
    client = FakeClient(repos)
    result = gs.search_candidates(client=client, now=NOW, max_results=3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Rate-limit backoff / retry (design.md section 10)
# ---------------------------------------------------------------------------


class FlakyClient:
    """Raises the given exception on the first N calls, then returns repos."""

    def __init__(self, repos, exc, fail_times=1):
        self._repos = repos
        self._exc = exc
        self._fail_times = fail_times
        self.call_count = 0

    def search_repositories(self, query, sort=None, order=None):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._exc
        return list(self._repos)


def _rate_limit_exc():
    return RateLimitExceededException(403, {"message": "rate limited"}, {})


def test_backoff_retries_after_rate_limit_then_succeeds():
    slept = []
    repos = [FakeRepo("owner/ok", open_issues=25, pushed_at=_old(9))]
    client = FlakyClient(repos, _rate_limit_exc(), fail_times=1)

    result = gs.search_candidates(
        client=client,
        now=NOW,
        sleep=lambda d: slept.append(d),
    )

    assert client.call_count == 2  # failed once, retried once
    assert [c.repo_full_name for c in result] == ["owner/ok"]
    assert len(slept) == 1  # slept exactly once before the retry
    assert slept[0] > 0


def test_with_backoff_gives_up_after_max_retries():
    calls = {"n": 0}
    slept = []

    def always_rate_limited():
        calls["n"] += 1
        raise _rate_limit_exc()

    with pytest.raises(RateLimitExceededException):
        gs.with_backoff(
            always_rate_limited,
            max_retries=3,
            base_delay=0.01,
            sleep=lambda d: slept.append(d),
            rng=lambda: 0.0,
        )

    # initial attempt + 3 retries = 4 calls; slept before each of the 3 retries
    assert calls["n"] == 4
    assert len(slept) == 3


def test_with_backoff_uses_exponential_delays():
    slept = []

    def always_rate_limited():
        raise _rate_limit_exc()

    with pytest.raises(RateLimitExceededException):
        gs.with_backoff(
            always_rate_limited,
            max_retries=3,
            base_delay=1.0,
            max_delay=100.0,
            sleep=lambda d: slept.append(d),
            rng=lambda: 0.0,  # no jitter for deterministic assertion
        )

    # base * 2**attempt for attempts 0,1,2 -> 1, 2, 4
    assert slept == [1.0, 2.0, 4.0]


def test_with_backoff_retries_secondary_rate_limit_403():
    slept = []
    calls = {"n": 0}

    def secondary_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise GithubException(403, {"message": "secondary rate limit"}, {})
        return "ok"

    result = gs.with_backoff(
        secondary_then_ok,
        base_delay=0.01,
        sleep=lambda d: slept.append(d),
        rng=lambda: 0.0,
    )
    assert result == "ok"
    assert calls["n"] == 2
    assert len(slept) == 1


def test_with_backoff_does_not_retry_non_rate_limit_error():
    def not_found():
        raise GithubException(404, {"message": "not found"}, {})

    with pytest.raises(GithubException):
        gs.with_backoff(not_found, sleep=lambda d: None)


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def test_search_requires_client_or_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError):
        gs.search_candidates(now=NOW)
