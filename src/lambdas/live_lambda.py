"""Live-discovery Lambda — read-only GitHub view for the demo dashboard.

Triggered by **API Gateway (GET /live)**. This is a hosting/demo companion to
:mod:`src.lambdas.dashboard_lambda`: where ``/state`` returns whatever the agent
pipeline has already written to DynamoDB, ``/live`` shows **real, current**
GitHub data so the dashboard always proves the point — the repositories on
screen are genuinely being curated from GitHub, not a hand-picked list.

What it does
------------
1. Runs the same distress-pattern search the Scanner uses
   (:func:`src.tools.github_search.search_candidates`) to find real
   likely-under-maintained repositories (``stars:10..500``, last commit older
   than the age threshold, ``open_issues >= floor``). A small **sample of ~10**
   repos is taken — enough to show real curation without loading everything.
2. For each sampled repo, fetches just its **latest open issue** with a single
   lightweight list call (newest first, pull requests filtered out). No deep
   scan — one page, first item.
3. Emits rows in the **same shape** as ``/state`` (``RepoState`` fields) plus a
   small ``live`` enrichment block (stars, language, the latest issue's number/
   title/url), so the existing frontend renders them with no special-casing.

Performance note (why only the latest issue, one call)
------------------------------------------------------
An earlier version reused ``get_repo_issues``, which scans up to 100 issues per
repo to rank by 👍. Across a batch that serially blew API Gateway's hard 29s
integration timeout. The demo only needs to show *a* real, current bug per repo,
so we fetch the single newest open issue with one paginated call and take the
first non-PR item. That keeps the whole request comfortably under the timeout.

Read-only boundary
------------------
This Lambda never writes DynamoDB, never enqueues SQS, never calls an agent or a
model. It only reads GitHub via a fine-grained PAT loaded from Secrets Manager
(reusing :func:`src.lambdas.scanner_lambda.load_github_token`). It is safe to
expose publicly: it returns only public-repo metadata.

Why a "discovered" status
-------------------------
Rows are labeled ``status = "discovered"`` because that is exactly what they
are from the system's point of view: real candidates the Scanner would enqueue.
This keeps the frontend's status vocabulary intact and truthful — nothing here
is simulated agent activity.

Configuration
-------------
- ``RESURRECTOR_GITHUB_TOKEN_SECRET_ID`` — Secrets Manager id for the PAT
  (default ``/resurrector/github-token``); ``GITHUB_TOKEN`` env wins for local.
- ``RESURRECTOR_LIVE_MAX_RESULTS`` — sample size of repos returned (default 10).
- Search knobs are inherited from :mod:`src.tools.github_search`
  (``RESURRECTOR_SEARCH_*``).
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from github import Auth, Github

from src.lambdas.scanner_lambda import load_github_token
from src.tools.github_search import RepoCandidate, search_candidates

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:  # pragma: no cover - Lambda configures the root logger
    LOGGER.setLevel(logging.INFO)

LIVE_MAX_RESULTS_ENV_VAR = "RESURRECTOR_LIVE_MAX_RESULTS"
# A sample of ~10 repos: enough to show real curation, small enough that one
# lightweight issue read per repo stays well inside API Gateway's 29s timeout.
DEFAULT_LIVE_MAX_RESULTS = 10

# Process-wide GitHub client, built once per container (cold start) so repeated
# invocations reuse the connection + cached token.
_GH_CLIENT: Any = None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _github_client(client: Optional[Github] = None) -> Github:
    """Return an injected client or the container-cached PAT-backed client."""
    global _GH_CLIENT
    if client is not None:
        return client
    if _GH_CLIENT is None:
        _GH_CLIENT = Github(auth=Auth.Token(load_github_token()))
    return _GH_CLIENT


def reset_clients() -> None:
    """Drop the cached GitHub client (used by tests)."""
    global _GH_CLIENT
    _GH_CLIENT = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_pull_request(issue: Any) -> bool:
    """True when a REST 'issue' is really a pull request (GitHub models PRs as issues)."""
    return getattr(issue, "pull_request", None) is not None


def _latest_open_issue(repo_obj: Any) -> Optional[dict[str, Any]]:
    """Return ``{number, title, url}`` for the repo's newest open issue, or None.

    One lightweight, paginated call sorted newest-first; we walk only the first
    handful of entries to skip any pull requests, then stop. No deep scan.
    """
    try:
        issues = repo_obj.get_issues(state="open", sort="created", direction="desc")
    except Exception:  # noqa: BLE001 - a repo whose issues can't be listed just gets no issue
        return None

    seen = 0
    for issue in issues:
        if seen >= 10:  # bound the walk; PRs are rarely the 10 newest items
            break
        seen += 1
        if _is_pull_request(issue):
            continue
        return {
            "number": int(getattr(issue, "number", 0) or 0),
            "title": getattr(issue, "title", None),
            "url": getattr(issue, "html_url", None),
        }
    return None


def _row_for(candidate: RepoCandidate, gh: Github) -> dict[str, Any]:
    """Build one dashboard row (RepoState shape + live enrichment) for a repo.

    Fetches the repo's single latest open issue (one call). Any per-repo read
    failure degrades to a row that still links to the repo's open-issues page,
    rather than dropping the repo or failing the whole response.
    """
    issue_number: Optional[int] = None
    issue_title: Optional[str] = None
    # Fallback: the repo's open issues, newest first.
    issue_url = f"{candidate.html_url}/issues?q=is%3Aissue+is%3Aopen+sort%3Acreated-desc"

    try:
        repo_obj = gh.get_repo(candidate.repo_full_name)
        latest = _latest_open_issue(repo_obj)
        if latest:
            issue_number = latest["number"] or None
            issue_title = latest["title"]
            if latest["url"]:
                issue_url = latest["url"]
    except Exception:  # noqa: BLE001 - one repo must not fail the batch
        LOGGER.warning(
            "could not read latest issue for %s", candidate.repo_full_name, exc_info=True
        )

    return {
        # RepoState-compatible fields (what the frontend already understands).
        "repo_full_name": candidate.repo_full_name,
        "status": "discovered",
        "issue_number": issue_number,
        "pr_number": None,
        "pr_url": None,
        "opened_at": None,
        "last_action_at": (
            candidate.last_commit_at.isoformat() if candidate.last_commit_at else None
        ),
        "maintainer_responded": False,
        "follow_up_count": 0,
        "complexity": None,
        "notes": "Live candidate from GitHub search — not yet engaged.",
        # Live enrichment the frontend can show in the detail drawer.
        "live": {
            "stars": candidate.stars,
            "open_issues": candidate.open_issues,
            "language": candidate.language,
            "html_url": candidate.html_url,
            "issue_title": issue_title,
            "issue_url": issue_url,
        },
    }


def get_live_data(
    *,
    client: Optional[Github] = None,
    max_results: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Run the distress search over a small sample and attach each repo's latest bug."""
    gh = _github_client(client)
    cap = (
        max_results
        if max_results is not None
        else _env_int(LIVE_MAX_RESULTS_ENV_VAR, DEFAULT_LIVE_MAX_RESULTS)
    )
    candidates = search_candidates(client=gh, max_results=cap)
    if not candidates:
        return []

    # The per-repo issue reads are I/O-bound HTTPS round-trips, so fan them out
    # concurrently rather than serially — this collapses ~N×(get_repo+get_issues)
    # into a few seconds and keeps the endpoint well inside API Gateway's 29s
    # timeout even on a cold start. Order is preserved via executor.map.
    workers = min(len(candidates), 10)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: _row_for(c, gh), candidates))


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            # Cache at the edge briefly so a demo refresh does not hammer the
            # GitHub search rate limit (30 req/min authenticated).
            "Cache-Control": "public, max-age=120",
        },
        "body": json.dumps(payload),
    }


def lambda_handler(
    event: Any,
    context: Any = None,  # noqa: ARG001
    *,
    client: Optional[Github] = None,
    max_results: Optional[int] = None,
) -> dict[str, Any]:
    """API Gateway ``GET /live`` entry point.

    Returns ``{"generated_at", "count", "repos": [...]}`` — the same envelope as
    ``/state`` — populated with a live sample of real GitHub candidates, each
    with its latest open bug. A failure (e.g. GitHub down, token invalid) is
    logged and returned as a 500 with a JSON error body; the handler never
    crashes.

    ``max_results`` may be overridden via the query string (``?limit=N``, capped
    at 15 to stay inside the request budget).
    """
    override = max_results
    if override is None and isinstance(event, dict):
        params = event.get("queryStringParameters") or {}
        raw = params.get("limit") if isinstance(params, dict) else None
        if raw is not None:
            try:
                override = max(1, min(15, int(raw)))
            except (TypeError, ValueError):
                override = None

    try:
        repos = get_live_data(client=client, max_results=override)
    except Exception:  # noqa: BLE001 - a live read failure must not crash the endpoint
        LOGGER.exception("live discovery failed")
        return _response(500, {"error": "failed to load live GitHub data"})

    return _response(
        200,
        {"generated_at": _now_iso(), "count": len(repos), "repos": repos},
    )
