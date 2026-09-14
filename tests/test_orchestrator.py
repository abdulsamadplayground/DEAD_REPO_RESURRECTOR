"""Unit tests for src.agents.orchestrator — the pipeline and the only state writer.

Fully offline: the three sub-agents, the state layer, and the follow-up poster
are all injected, so no network, no AWS, no live Bedrock. strands IS installed
in this environment, so the wiring tests build a real Agent and must pass (not
skip).

Covers:
  - run(): in_progress at start; gate refuse → skipped_complex (Engineer never
    called); proceed → Engineer called (REQ-2.4); engineer failure → fix_failed
    (Communicator never called); full success → pr_opened with the REQ-4.4
    fields and a 7-day follow_up intent (REQ-6.1); a raising sub-agent →
    terminal state, no crash (design §10).
  - the only-writer invariant (spy on the injected transition) and an AST test
    that the sub-agents don't import dynamo_tools while the Orchestrator does.
  - handle_reply() maintainer activity: merged → success (REQ-5.2); closed →
    rejected (REQ-5.4); question → reply + maintainer_responded, stays pr_opened
    (REQ-5.3); not pr_opened → no-op; non-maintainer comment.
  - handle_reply() timers: 7-day → follow-up + increment + SNS + 14-day intent
    (REQ-6.2/6.4); 7-day but responded → no-op; 14-day → dormant + increment +
    SNS (REQ-6.3); 14-day but responded → no-op.
  - strands wiring: build_orchestrator() → Agent with exactly the 5 tools.
  - tone/safety on Orchestrator-generated text; JSON-serializability.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from src.agents import orchestrator
from src.tools import pr_templates
from src.tools.complexity_scorer import ComplexityResult, GateDecision
from src.tools.github_comms import CommentResult, PROpenResult


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeAnalystReport:
    """Mirror of AnalystReport's surface used by the Orchestrator."""

    def __init__(self, *, proceed, complexity="trivial", confidence=0.9,
                 issue_number=42, issue_title="Crash on empty config",
                 approach="Guard the empty case in loader.py", files=None,
                 status=None, reason="ok"):
        self.repo_full_name = "owner/repo"
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.issue_url = "https://github.com/owner/repo/issues/42"
        self.result = ComplexityResult(
            complexity=complexity,
            confidence=confidence,
            files_affected=files or ["loader.py"],
            approach=approach,
        )
        self.source = "model"
        self.gate = GateDecision(
            proceed=proceed,
            status=status,
            reason=reason,
            threshold=0.6,
            complexity=complexity,
            confidence=confidence,
        )
        self.notes = reason

    def to_dict(self):
        return {
            "repo_full_name": self.repo_full_name,
            "issue_number": self.issue_number,
            "gate": self.gate.to_dict(),
            "result": self.result.to_dict(),
        }


class FakeEngineerResult:
    def __init__(self, *, success=True, branch="resurrector/fix-issue-42",
                 reason="pushed 1 file(s)", notes="Guarded the empty case",
                 test_result=None):
        self.repo_full_name = "owner/repo"
        self.issue_number = 42
        self.success = success
        self.branch = branch
        self.reason = reason
        self.notes = notes
        self.test_result = test_result or {"outcome": "passed"}
        self.recommended_status = None if success else "fix_failed"

    def to_dict(self):
        return {
            "success": self.success,
            "branch": self.branch,
            "reason": self.reason,
            "recommended_status": self.recommended_status,
        }


class FakeRecord:
    """Mirror of dynamo_tools.RepoState used by handle_reply."""

    def __init__(self, *, status="pr_opened", maintainer_responded=False,
                 pr_number=321, issue_number=42, follow_up_count=0):
        self.repo_full_name = "owner/repo"
        self.status = status
        self.maintainer_responded = maintainer_responded
        self.pr_number = pr_number
        self.pr_url = f"https://github.com/owner/repo/pull/{pr_number}"
        self.issue_number = issue_number
        self.follow_up_count = follow_up_count


class TransitionSpy:
    """Records every state write so the only-writer invariant is checkable."""

    def __init__(self, record=None):
        self.calls = []
        self._record = record

    def __call__(self, repo, status, *, table_name=None, **updates):
        self.calls.append((repo, status, updates))
        rec = self._record or FakeRecord(status=status)
        rec.status = status
        for key, value in updates.items():
            setattr(rec, key, value)
        return rec

    @property
    def statuses(self):
        return [c[1] for c in self.calls]


class IncrementSpy:
    def __init__(self, start=0):
        self.count = start
        self.calls = []

    def __call__(self, repo, *, new_status=None, table_name=None, **updates):
        self.count += 1
        self.calls.append((repo, new_status, updates))
        rec = FakeRecord(status=new_status or "pr_opened",
                         follow_up_count=self.count)
        return rec


def _pr_result(success=True, reason="opened"):
    return PROpenResult(
        repo_full_name="owner/repo",
        success=success,
        reason=reason,
        pr_number=321 if success else None,
        pr_url="https://github.com/owner/repo/pull/321" if success else None,
        issue_number=42,
        opened_at="2024-02-02T00:00:00+00:00" if success else None,
        branch="resurrector/fix-issue-42",
        created=True,
    )


def _comment_result(target="issue"):
    return CommentResult(
        repo_full_name="owner/repo",
        success=True,
        target=target,
        target_number=42,
        comment_id=1,
        comment_url="https://github.com/owner/repo/issues/42#c1",
    )


# ---------------------------------------------------------------------------
# run() — forward pipeline
# ---------------------------------------------------------------------------


def test_run_starts_with_in_progress_then_gate_refuse_skips(monkeypatch):
    """Gate refuse → skipped_complex; Engineer is never called (REQ-2.3)."""
    spy = TransitionSpy()
    engineer_called = []

    def analyze(repo):
        return FakeAnalystReport(
            proceed=False, complexity="complex",
            status="skipped_complex", reason="complexity is 'complex'",
        )

    def implement(repo, issue_number, **kw):
        engineer_called.append(True)
        return FakeEngineerResult()

    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=analyze, implement=implement,
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )

    assert spy.statuses == ["in_progress", "skipped_complex"]
    assert result.status == "skipped_complex"
    assert result.stage_reached == "analyst"
    assert engineer_called == []


def test_run_proceeds_to_engineer_when_gate_allows(monkeypatch):
    """REQ-2.4: trivial|moderate → Engineer runs."""
    spy = TransitionSpy()
    engineer_called = []

    def implement(repo, issue_number, **kw):
        engineer_called.append((repo, issue_number))
        return FakeEngineerResult(success=True)

    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=implement,
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )
    assert engineer_called == [("owner/repo", 42)]


def test_run_engineer_failure_records_fix_failed_and_skips_communicator():
    spy = TransitionSpy()
    announce_called = []

    def announce(*a, **k):
        announce_called.append(True)
        return _pr_result(), _comment_result()

    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda repo, n, **kw: FakeEngineerResult(
            success=False, reason="tests failed"
        ),
        announce=announce,
        transition=spy,
    )
    assert spy.statuses == ["in_progress", "fix_failed"]
    assert result.status == "fix_failed"
    assert result.stage_reached == "engineer"
    assert announce_called == []


def test_run_full_success_records_pr_opened_with_req_4_4_fields():
    spy = TransitionSpy()
    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda repo, n, **kw: FakeEngineerResult(success=True),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )
    assert spy.statuses == ["in_progress", "pr_opened"]
    # REQ-4.4 attributes on the pr_opened write.
    _, _, updates = spy.calls[-1]
    assert updates["pr_number"] == 321
    assert updates["pr_url"] == "https://github.com/owner/repo/pull/321"
    assert updates["issue_number"] == 42
    assert updates["opened_at"] == "2024-02-02T00:00:00+00:00"

    assert result.status == "pr_opened"
    assert result.stage_reached == "done"
    # REQ-6.1: a 7-day follow-up intent is signalled, not performed.
    assert result.follow_up == {
        "delay_seconds": 604800,
        "message_type": "follow_up",
        "stage": 7,
    }


def test_run_pr_open_failure_records_fix_failed():
    spy = TransitionSpy()
    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda repo, n, **kw: FakeEngineerResult(success=True),
        announce=lambda *a, **k: (_pr_result(success=False, reason="422"), None),
        transition=spy,
    )
    assert spy.statuses == ["in_progress", "fix_failed"]
    assert result.stage_reached == "communicator"


def test_run_analyst_exception_is_recorded_not_raised():
    spy = TransitionSpy()

    def boom(repo):
        raise RuntimeError("bedrock down")

    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=boom,
        implement=lambda *a, **k: FakeEngineerResult(),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )
    assert spy.statuses == ["in_progress", "skipped_complex"]
    assert result.status == "skipped_complex"


def test_run_engineer_exception_is_recorded_not_raised():
    spy = TransitionSpy()

    def boom(repo, n, **kw):
        raise RuntimeError("kaboom")

    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=boom,
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )
    assert spy.statuses == ["in_progress", "fix_failed"]
    assert result.stage_reached == "engineer"


def test_run_missing_repo_raises():
    with pytest.raises(ValueError):
        orchestrator.run({}, transition=TransitionSpy())


def test_run_result_is_json_serializable_in_every_branch():
    for analyze, implement, announce in [
        (lambda r: FakeAnalystReport(proceed=False, complexity="complex",
                                     status="skipped_complex"),
         lambda r, n, **k: FakeEngineerResult(),
         lambda *a, **k: (_pr_result(), _comment_result())),
        (lambda r: FakeAnalystReport(proceed=True),
         lambda r, n, **k: FakeEngineerResult(success=False),
         lambda *a, **k: (_pr_result(), _comment_result())),
        (lambda r: FakeAnalystReport(proceed=True),
         lambda r, n, **k: FakeEngineerResult(success=True),
         lambda *a, **k: (_pr_result(), _comment_result())),
    ]:
        result = orchestrator.run(
            {"repo_full_name": "owner/repo"},
            analyze=analyze, implement=implement, announce=announce,
            transition=TransitionSpy(),
        )
        # Round-trips cleanly.
        json.loads(result.to_json())


# ---------------------------------------------------------------------------
# Only-writer invariant
# ---------------------------------------------------------------------------


def test_only_writer_all_state_writes_go_through_the_injected_seam():
    """Every state write in a full success flow goes through the one seam."""
    spy = TransitionSpy()
    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda repo, n, **kw: FakeEngineerResult(success=True),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=spy,
    )
    # Exactly two writes, both through the spy: in_progress then pr_opened.
    assert spy.statuses == ["in_progress", "pr_opened"]


def _imported_modules(module):
    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_orchestrator_imports_dynamo_tools_but_subagents_do_not():
    from src.agents import analyst, communicator, engineer

    assert any("dynamo_tools" in n for n in _imported_modules(orchestrator))
    for sub in (analyst, engineer, communicator):
        assert not any("dynamo_tools" in n for n in _imported_modules(sub))


# ---------------------------------------------------------------------------
# handle_reply() — maintainer activity (REQ-5.*)
# ---------------------------------------------------------------------------


def test_handle_reply_merged_sets_success_and_no_follow_up():
    spy = TransitionSpy(record=FakeRecord())
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "pr_number": 321,
         "merged": True, "action": "closed", "state": "closed",
         "is_maintainer": True},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        transition=spy,
    )
    assert result.status == "success"
    assert spy.statuses == ["success"]
    assert result.follow_up is None


def test_handle_reply_closed_unmerged_sets_rejected():
    spy = TransitionSpy(record=FakeRecord())
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "pr_number": 321,
         "merged": False, "action": "closed", "state": "closed",
         "is_maintainer": True},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        transition=spy,
    )
    assert result.status == "rejected"
    assert spy.statuses == ["rejected"]


def test_handle_reply_question_posts_reply_and_marks_responded():
    spy = TransitionSpy(record=FakeRecord())
    reply_calls = []

    def reply(repo, pr_number, **kw):
        reply_calls.append((repo, pr_number, kw.get("text")))
        return object(), _comment_result(target="pull_request")

    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "pr_number": 321,
         "text": "Why did you change the default here?", "is_maintainer": True},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        transition=spy, reply=reply,
    )
    assert result.status == "pr_opened"  # REQ-5.3: stays open
    assert result.maintainer_responded is True
    assert reply_calls and reply_calls[0][1] == 321
    # maintainer_responded=True is persisted (stops the 7-day nudge).
    _, _, updates = spy.calls[-1]
    assert updates["maintainer_responded"] is True


def test_handle_reply_noop_when_not_pr_opened():
    spy = TransitionSpy()
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "merged": True, "is_maintainer": True},
        read_state_fn=lambda repo, table_name=None: FakeRecord(status="success"),
        transition=spy,
    )
    assert spy.calls == []
    assert "no-op" in result.notes


def test_handle_reply_non_maintainer_comment_no_state_change():
    spy = TransitionSpy(record=FakeRecord())
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "pr_number": 321,
         "text": "I also hit this bug.", "is_maintainer": False},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        transition=spy,
    )
    assert spy.calls == []
    assert result.status == "pr_opened"


# ---------------------------------------------------------------------------
# handle_reply() — timers (REQ-6.2/6.3/6.4)
# ---------------------------------------------------------------------------


def test_handle_reply_7day_posts_follow_up_increments_and_signals():
    inc = IncrementSpy()
    posted = []

    def poster(repo, pr_number, *, stage, client=None, token=None):
        posted.append((repo, pr_number, stage))
        return _comment_result(target="pull_request")

    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 7},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        increment=inc, post_follow_up_fn=poster,
    )
    assert posted == [("owner/repo", 321, 7)]
    assert inc.count == 1  # REQ-6.4 increment
    assert result.follow_up == {
        "delay_seconds": 1209600, "message_type": "follow_up", "stage": 14,
    }
    assert result.sns is not None  # REQ-6.4 SNS signal
    assert result.status == "pr_opened"


def test_handle_reply_7day_noop_when_maintainer_responded():
    inc = IncrementSpy()
    posted = []
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 7},
        read_state_fn=lambda repo, table_name=None: FakeRecord(
            maintainer_responded=True
        ),
        increment=inc, post_follow_up_fn=lambda *a, **k: posted.append(True),
    )
    assert posted == []
    assert inc.count == 0
    assert result.sns is None
    assert "no-op" in result.notes


def test_handle_reply_14day_marks_dormant_and_increments():
    inc = IncrementSpy()
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 14},
        read_state_fn=lambda repo, table_name=None: FakeRecord(follow_up_count=1),
        increment=inc,
        post_follow_up_fn=lambda *a, **k: pytest.fail("no comment at 14d"),
    )
    assert result.status == "dormant"  # REQ-6.3
    assert inc.count == 1
    assert inc.calls[0][1] == "dormant"
    assert result.sns is not None
    assert result.follow_up is None  # stop follow-up


def test_handle_reply_14day_noop_when_responded():
    inc = IncrementSpy()
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 14},
        read_state_fn=lambda repo, table_name=None: FakeRecord(
            maintainer_responded=True
        ),
        increment=inc,
    )
    assert inc.count == 0
    assert "no-op" in result.notes


def test_handle_reply_result_is_json_serializable():
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 7},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        increment=IncrementSpy(),
        post_follow_up_fn=lambda *a, **k: _comment_result(target="pull_request"),
    )
    json.loads(result.to_json())


# ---------------------------------------------------------------------------
# Tone / safety on Orchestrator-generated text
# ---------------------------------------------------------------------------


def test_generated_pr_narrative_and_sns_have_no_banned_words():
    captured = {}

    def announce(repo, **kw):
        captured.update(kw)
        return _pr_result(), _comment_result()

    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True, approach=""),
        implement=lambda r, n, **kw: FakeEngineerResult(success=True, notes=""),
        announce=announce,
        transition=TransitionSpy(),
    )
    text = " ".join(
        str(captured.get(k, "")) for k in ("what_changed", "why", "how_to_test")
    ).lower()
    for banned in pr_templates.FORBIDDEN_TERMS:
        assert banned not in text

    # SNS escalation summaries too.
    result = orchestrator.handle_reply(
        {"repo_full_name": "owner/repo", "message_type": "follow_up", "stage": 14},
        read_state_fn=lambda repo, table_name=None: FakeRecord(),
        increment=IncrementSpy(),
    )
    sns_text = (result.sns["subject"] + " " + result.sns["message"]).lower()
    for banned in pr_templates.FORBIDDEN_TERMS:
        assert banned not in sns_text


def test_system_prompt_bakes_in_safe_wording():
    prompt = orchestrator.ORCHESTRATOR_SYSTEM_PROMPT.lower()
    assert "appears to have reduced maintenance activity" in prompt


# ---------------------------------------------------------------------------
# strands wiring (strands IS installed — must run, not skip)
# ---------------------------------------------------------------------------


def test_build_orchestrator_returns_a_real_agent_with_five_tools():
    from strands import Agent

    agent = orchestrator.build_orchestrator()
    assert isinstance(agent, Agent)
    assert len(orchestrator.ORCHESTRATOR_TOOLS) == 5


def test_orchestrator_tools_are_the_five_design_tools():
    names = {getattr(t, "__name__", None) for t in orchestrator.ORCHESTRATOR_TOOLS}
    # The @tool wrapper may rename; check via the tuple identity instead.
    assert len(orchestrator.ORCHESTRATOR_TOOLS) == 5


def test_read_state_tool_returns_json(monkeypatch):
    from src.tools import dynamo_tools

    monkeypatch.setattr(
        dynamo_tools, "read_state",
        lambda repo: dynamo_tools.RepoState(repo_full_name=repo, status="pr_opened"),
    )
    out = orchestrator.read_state("owner/repo")
    data = json.loads(out)
    assert data["status"] == "pr_opened"


def test_read_state_tool_returns_null_when_absent(monkeypatch):
    from src.tools import dynamo_tools

    monkeypatch.setattr(dynamo_tools, "read_state", lambda repo: None)
    assert json.loads(orchestrator.read_state("owner/repo")) is None


def test_write_state_tool_delegates_to_transition(monkeypatch):
    from src.tools import dynamo_tools

    calls = []

    def fake_transition(repo, status, **updates):
        calls.append((repo, status, updates))
        return dynamo_tools.RepoState(repo_full_name=repo, status=status)

    monkeypatch.setattr(dynamo_tools, "transition", fake_transition)
    out = orchestrator.write_state("owner/repo", "pr_opened", notes="hi")
    assert calls == [("owner/repo", "pr_opened", {"notes": "hi"})]
    assert json.loads(out)["status"] == "pr_opened"


# ---------------------------------------------------------------------------
# run() — model pacing between Analyst and Engineer (rate-limit safety)
# ---------------------------------------------------------------------------


def test_run_paces_once_between_analyst_and_engineer():
    """pace_seconds>0 → pace_sleep(pace) fires exactly once, after a successful
    Analyst and before the Engineer."""
    events = []

    def analyze(repo):
        events.append("analyze")
        return FakeAnalystReport(proceed=True)

    def implement(repo, n, **kw):
        events.append("implement")
        return FakeEngineerResult(success=True)

    def pace_sleep(seconds):
        events.append(("pace", seconds))

    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=analyze,
        implement=implement,
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=TransitionSpy(),
        pace_sleep=pace_sleep,
        pace_seconds=65,
    )

    # Exactly one pause, with the configured duration, ordered strictly between
    # the Analyst and the Engineer.
    assert events == ["analyze", ("pace", 65), "implement"]


def test_run_paces_from_env_var(monkeypatch):
    """Env var supplies the pace when the arg is omitted."""
    monkeypatch.setenv(orchestrator.MODEL_PACE_ENV_VAR, "65")
    slept = []

    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda r, n, **kw: FakeEngineerResult(success=True),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=TransitionSpy(),
        pace_sleep=lambda s: slept.append(s),
    )
    assert slept == [65]


def test_run_does_not_pace_when_gate_refuses():
    """Gate refuse → Engineer never runs → no pause, even with pace configured."""
    slept = []
    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(
            proceed=False, complexity="complex", status="skipped_complex",
        ),
        implement=lambda r, n, **kw: FakeEngineerResult(),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=TransitionSpy(),
        pace_sleep=lambda s: slept.append(s),
        pace_seconds=65,
    )
    assert slept == []


def test_run_does_not_pace_when_analyst_errors():
    """Analyst exception → skipped_complex before the Engineer → no pause."""
    slept = []

    def boom(repo):
        raise RuntimeError("bedrock down")

    orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=boom,
        implement=lambda r, n, **kw: FakeEngineerResult(),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=TransitionSpy(),
        pace_sleep=lambda s: slept.append(s),
        pace_seconds=65,
    )
    assert slept == []


def test_run_default_pace_zero_does_not_sleep(monkeypatch):
    """No env, no arg → resolved pace is 0 → pace_sleep is never called."""
    monkeypatch.delenv(orchestrator.MODEL_PACE_ENV_VAR, raising=False)
    slept = []
    result = orchestrator.run(
        {"repo_full_name": "owner/repo"},
        analyze=lambda repo: FakeAnalystReport(proceed=True),
        implement=lambda r, n, **kw: FakeEngineerResult(success=True),
        announce=lambda *a, **k: (_pr_result(), _comment_result()),
        transition=TransitionSpy(),
        pace_sleep=lambda s: slept.append(s),
    )
    # Behaviour is identical to the no-pacing baseline.
    assert slept == []
    assert result.status == "pr_opened"
    assert result.stage_reached == "done"


def test_model_pace_seconds_parsing(monkeypatch):
    """unset → 0, '65' → 65, 'bad' → 0."""
    monkeypatch.delenv(orchestrator.MODEL_PACE_ENV_VAR, raising=False)
    assert orchestrator._model_pace_seconds() == 0

    monkeypatch.setenv(orchestrator.MODEL_PACE_ENV_VAR, "65")
    assert orchestrator._model_pace_seconds() == 65

    monkeypatch.setenv(orchestrator.MODEL_PACE_ENV_VAR, "bad")
    assert orchestrator._model_pace_seconds() == 0

    monkeypatch.setenv(orchestrator.MODEL_PACE_ENV_VAR, "")
    assert orchestrator._model_pace_seconds() == 0
