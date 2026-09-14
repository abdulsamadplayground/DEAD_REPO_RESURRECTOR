"""Unit tests for src.agents.communicator (Communicator sub-agent).

Fully offline: fake PyGithub objects and injected model runners; no network, no
AWS, no live Bedrock. strands IS installed in this environment, so the wiring
tests build a real Agent and must pass (not skip).

Covers the four design.md-section-3 tools, the REQ-4 happy path (announce_pr),
the REQ-5.3 reply path (reply_to_maintainer), the agent-as-tool entry point,
the strands wiring, and the design-section-3 boundaries (no DynamoDB writes, no
branch/file writes) asserted against the AST.
"""

from __future__ import annotations

import json

import pytest

from src.agents import communicator
from src.tools import github_comms


# ---------------------------------------------------------------------------
# Test doubles (mirror tests/test_github_comms.py)
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
        return FakeComment(comment_id=100 + self.number)


class FakeHead:
    def __init__(self, ref):
        self.ref = ref


class FakePull:
    def __init__(self, number, head_ref):
        self.number = number
        self.head = FakeHead(head_ref)
        self.html_url = f"https://github.com/o/r/pull/{number}"


class FakeRepo:
    def __init__(self, *, default_branch="main"):
        self.default_branch = default_branch
        self.full_name = "owner/repo"
        self.created_pulls = []
        self.issues = {}

    def create_pull(self, title, body, head, base):
        self.created_pulls.append(
            {"title": title, "body": body, "head": head, "base": base}
        )
        return FakePull(number=321, head_ref=head)

    def get_pulls(self, state=None):
        return []

    def get_issue(self, number):
        issue = FakeIssue(number)
        self.issues[number] = issue
        return issue


# ---------------------------------------------------------------------------
# Tool adapters return JSON strings
# ---------------------------------------------------------------------------


def test_open_pr_tool_returns_json_with_pr_fields(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(
        github_comms, "_resolve_repo", lambda *a, **k: repo
    )
    out = communicator.open_pr(
        "owner/repo",
        "resurrector/fix-issue-42",
        42,
        "Crash on empty config",
        "Guarded the empty case.",
        why="Empty configs crashed the loader.",
        how_to_test="Run pytest.",
    )
    data = json.loads(out)
    assert data["success"] is True
    assert data["pr_number"] == 321
    assert data["issue_number"] == 42
    assert repo.created_pulls[0]["title"] == (
        "[Resurrector] Fix: Crash on empty config (closes #42)"
    )


def test_open_pr_tool_reports_error_as_json(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(github_comms, "open_pr", boom)
    out = communicator.open_pr(
        "owner/repo", "resurrector/fix-issue-1", 1, "t", "c"
    )
    data = json.loads(out)
    assert "error" in data
    assert "RuntimeError" in data["error"]


def test_post_issue_comment_tool_generates_the_req_4_3_wording(monkeypatch):
    captured = {}

    def fake_post(repo_full_name, issue_number, body, **kwargs):
        captured["body"] = body
        return github_comms.CommentResult(
            repo_full_name=repo_full_name,
            success=True,
            target="issue",
            target_number=issue_number,
            comment_url="https://github.com/o/r/issues/42#c1",
        )

    monkeypatch.setattr(github_comms, "post_issue_comment", fake_post)
    out = communicator.post_issue_comment(
        "owner/repo", 42, "https://github.com/o/r/pull/321"
    )
    data = json.loads(out)
    assert data["success"] is True
    assert "https://github.com/o/r/pull/321" in captured["body"]
    for banned in ("dead", "abandoned", "unmaintained"):
        assert banned not in captured["body"].lower()


def test_post_pr_comment_tool_returns_json(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(github_comms, "_resolve_repo", lambda *a, **k: repo)
    out = communicator.post_pr_comment("owner/repo", 321, "Here is the answer.")
    data = json.loads(out)
    assert data["success"] is True
    assert data["target"] == "pr"
    assert data["target_number"] == 321


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"merged": True}, "merged"),
        ({"state": "closed"}, "closed"),
        ({"text": "Why did you change this?"}, "question"),
        ({"text": "thanks!"}, "comment"),
        ({}, "other"),
    ],
)
def test_classify_maintainer_reply_tool(kwargs, expected):
    out = communicator.classify_maintainer_reply(**kwargs)
    data = json.loads(out)
    assert data["classification"] == expected


# ---------------------------------------------------------------------------
# announce_pr — REQ-4 happy path
# ---------------------------------------------------------------------------


def test_announce_pr_opens_pr_then_comments_on_issue():
    repo = FakeRepo()
    pr_result, comment_result = communicator.announce_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-42",
        issue_number=42,
        issue_title="Crash on empty config",
        what_changed="Guarded the empty case.",
        why="Empty configs crashed the loader.",
        how_to_test="Run pytest.",
        repo=repo,
    )
    assert pr_result.success is True
    assert comment_result is not None
    assert comment_result.success is True
    # the issue comment links the PR that was opened
    posted = repo.issues[42].comments_posted[0]
    assert pr_result.pr_url in posted


def test_announce_pr_skips_comment_when_pr_fails():
    repo = FakeRepo(default_branch=None)  # forces a soft open_pr failure
    pr_result, comment_result = communicator.announce_pr(
        "owner/repo",
        head_branch="resurrector/fix-issue-1",
        issue_number=1,
        issue_title="t",
        what_changed="c",
        repo=repo,
    )
    assert pr_result.success is False
    assert comment_result is None
    assert repo.created_pulls == []


# ---------------------------------------------------------------------------
# reply_to_maintainer — REQ-5.3 reply path
# ---------------------------------------------------------------------------


def test_reply_to_maintainer_replies_only_to_questions():
    repo = FakeRepo()
    classification, comment = communicator.reply_to_maintainer(
        "owner/repo",
        321,
        text="Why did you touch the cache?",
        reply_body="Because the cache was the source of the stale read.",
        repo=repo,
    )
    assert classification.classification == "question"
    assert comment is not None
    assert comment.success is True
    assert repo.issues[321].comments_posted[0].startswith("Because the cache")


def test_reply_to_maintainer_no_comment_for_merged():
    repo = FakeRepo()
    classification, comment = communicator.reply_to_maintainer(
        "owner/repo", 321, merged=True, repo=repo
    )
    assert classification.classification == "merged"
    assert comment is None
    assert repo.issues == {}


def test_reply_to_maintainer_uses_injected_model_for_question():
    repo = FakeRepo()
    classification, comment = communicator.reply_to_maintainer(
        "owner/repo",
        321,
        text="How does this handle unicode?",
        run_model=lambda prompt: "It decodes as UTF-8 and falls back cleanly.",
        repo=repo,
    )
    assert classification.classification == "question"
    assert comment is not None
    assert "UTF-8" in repo.issues[321].comments_posted[0]


# ---------------------------------------------------------------------------
# Agent-as-tool entry point
# ---------------------------------------------------------------------------


def test_communicator_agent_returns_pr_and_comment_json(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(github_comms, "_resolve_repo", lambda *a, **k: repo)
    out = communicator.communicator_agent(
        "owner/repo",
        "resurrector/fix-issue-42",
        42,
        "Crash on empty config",
        "Guarded the empty case.",
        why="Empty configs crashed.",
        how_to_test="pytest",
    )
    data = json.loads(out)
    assert data["pr"]["success"] is True
    assert data["issue_comment"]["success"] is True


# ---------------------------------------------------------------------------
# strands wiring (strands IS installed — must run, not skip)
# ---------------------------------------------------------------------------


def test_build_communicator_returns_a_real_agent():
    from strands import Agent

    agent = communicator.build_communicator()
    assert isinstance(agent, Agent)


def test_build_communicator_accepts_the_four_design_tools():
    from strands import Agent

    agent = communicator.build_communicator()
    assert isinstance(agent, Agent)
    assert len(communicator.COMMUNICATOR_TOOLS) == 4


def test_build_communicator_uses_the_communicator_system_prompt():
    agent = communicator.build_communicator()
    assert communicator.COMMUNICATOR_SYSTEM_PROMPT in str(agent.system_prompt)


def test_system_prompt_bakes_in_the_safe_wording():
    prompt = communicator.COMMUNICATOR_SYSTEM_PROMPT.lower()
    assert "appears to have reduced maintenance activity" in prompt
    # the prompt tells the model never to use the banned words, but must not
    # itself instruct in a way that leaves a bare banned adjective describing
    # the repo — it only appears inside the explicit prohibition list.
    assert "never call" in prompt


# ---------------------------------------------------------------------------
# Boundaries (design.md section 3), asserted against the AST
# ---------------------------------------------------------------------------


def _imported_modules(module):
    import ast
    import inspect

    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _called_names(module):
    import ast
    import inspect

    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_communicator_does_not_write_dynamodb():
    # The Orchestrator is the only component that persists state (design.md §3).
    assert not any("dynamo_tools" in name for name in _imported_modules(communicator))
    called = _called_names(communicator)
    assert "write_state" not in called
    assert "transition" not in called


def test_communicator_does_not_create_branches_or_write_files():
    # The Engineer is the only agent that writes code / creates branches (§3).
    called = _called_names(communicator)
    for forbidden in (
        "create_branch",
        "create_git_ref",
        "create_git_tree",
        "create_git_commit",
        "update_file",
        "create_file",
    ):
        assert forbidden not in called


# ---------------------------------------------------------------------------
# post_follow_up (REQ-6.2 wording, routed through the Communicator)
# ---------------------------------------------------------------------------


def test_post_follow_up_posts_the_warm_nudge(monkeypatch):
    from src.tools import pr_templates

    captured = {}

    def spy_post_pr_comment(repo_full_name, pr_number, body, **kwargs):
        captured["repo"] = repo_full_name
        captured["pr_number"] = pr_number
        captured["body"] = body
        return github_comms.CommentResult(
            repo_full_name=repo_full_name,
            success=True,
            target="pull_request",
            target_number=pr_number,
            comment_id=7,
            comment_url="https://github.com/o/r/pull/9#c7",
        )

    monkeypatch.setattr(github_comms, "post_pr_comment", spy_post_pr_comment)

    result = communicator.post_follow_up("owner/repo", 9, stage=7)

    assert result.success is True
    assert captured["repo"] == "owner/repo"
    assert captured["pr_number"] == 9
    assert captured["body"] == pr_templates.follow_up_comment(7)
    # Tone: the nudge never uses a banned word.
    lowered = captured["body"].lower()
    for banned in pr_templates.FORBIDDEN_TERMS:
        assert banned not in lowered
