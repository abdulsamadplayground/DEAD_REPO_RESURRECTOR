"""Unit tests for src.agents.analyst.

Fully offline. No GitHub, no Bedrock, no AWS. The repo context is built by hand
and the "model" is a plain callable injected as ``run_model``, which is exactly
the seam design.md section 10 needs: when the model is unavailable or its output
is unparsable, the Analyst must fall back to the deterministic scorer.

``strands`` is not required. The two tests that genuinely need it use
``pytest.importorskip`` and skip cleanly when it is absent.
"""

from __future__ import annotations

import json

import pytest

import src.agents.analyst as analyst
from src.tools.complexity_scorer import SKIPPED_STATUS
from src.tools.github_tools import (
    CommitSummary,
    IssueSummary,
    RepoContext,
    RepoStructure,
    TreeEntry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WELL_SPECIFIED_BODY = """\
### Steps to reproduce

1. Call `parse("")`

### Actual behaviour

```
Traceback (most recent call last):
  File "src/parser.py", line 42, in parse
    return tokens[0]
TypeError: 'NoneType' object is not subscriptable
```

### Expected behaviour

It should return an empty list.
"""


def _issue(number=7, title=None, body=WELL_SPECIFIED_BODY, thumbs_up=12, labels=("bug",)):
    return IssueSummary(
        number=number,
        title=title or "TypeError in src/parser.py line 42 on empty input",
        body=body,
        thumbs_up=thumbs_up,
        total_reactions=thumbs_up,
        comments=3,
        labels=list(labels),
        html_url=f"https://github.com/owner/repo/issues/{number}",
    )


def _context(issues=None):
    return RepoContext(
        repo_full_name="owner/repo",
        default_branch="main",
        primary_language="Python",
        readme="# Project\n\nA parser.",
        structure=RepoStructure(
            entries=[TreeEntry("README.md", "blob", 12), TreeEntry("src/parser.py", "blob", 900)]
        ),
        issues=[_issue()] if issues is None else issues,
        recent_commits=[CommitSummary("abc1234", "fix: thing", "Ada", "2022-05-01T00:00:00+00:00")],
    )


MODEL_JSON = json.dumps(
    {
        "complexity": "moderate",
        "confidence": 0.81,
        "files_affected": ["src/parser.py"],
        "approach": "guard the empty-token case and add a regression test",
    }
)


# ---------------------------------------------------------------------------
# Import safety without strands
# ---------------------------------------------------------------------------


def test_module_exposes_strands_availability_flag():
    # Importing the module must never crash, with or without strands.
    assert isinstance(analyst.STRANDS_AVAILABLE, bool)


def test_tool_decorator_is_identity_without_strands():
    if analyst.STRANDS_AVAILABLE:
        pytest.skip("strands is installed; the real decorator applies")

    def plain(x):
        return x

    assert analyst.tool(plain) is plain


def test_build_analyst_raises_clear_error_without_strands():
    if analyst.STRANDS_AVAILABLE:
        pytest.skip("strands is installed; see the strands-gated test")
    with pytest.raises(analyst.StrandsUnavailableError) as exc:
        analyst.build_analyst()
    assert "strands-agents is not installed" in str(exc.value)


def test_build_analyst_with_strands_installed():
    pytest.importorskip("strands")
    agent = analyst.build_analyst()
    assert agent is not None


def test_analyst_tool_set_has_the_four_design_tools():
    pytest.importorskip("strands")
    assert len(analyst.ANALYST_TOOLS) == 4


# ---------------------------------------------------------------------------
# Model path (REQ-2.2)
# ---------------------------------------------------------------------------


def test_model_output_is_used_when_parsable():
    report = analyst.analyze_repo(
        "owner/repo", context=_context(), run_model=lambda prompt: MODEL_JSON
    )
    assert report.source == "model"
    assert report.result.complexity == "moderate"
    assert report.result.confidence == 0.81
    assert report.result.files_affected == ["src/parser.py"]
    assert report.issue_number == 7
    assert report.gate.proceed is True


def test_model_output_wrapped_in_prose_is_still_used():
    noisy = "Here is my assessment:\n```json\n" + MODEL_JSON + "\n```\nLet me know."
    report = analyst.analyze_repo("owner/repo", context=_context(), run_model=lambda p: noisy)
    assert report.source == "model"
    assert report.result.complexity == "moderate"


def test_prompt_contains_every_req_2_1_input():
    captured = {}

    def fake_model(prompt):
        captured["prompt"] = prompt
        return MODEL_JSON

    analyst.analyze_repo("owner/repo", context=_context(), run_model=fake_model)
    prompt = captured["prompt"]
    assert "owner/repo" in prompt
    assert "Python" in prompt  # primary language
    assert "# Project" in prompt  # README
    assert "src/parser.py" in prompt  # structure
    assert "abc1234" in prompt  # recent commits
    assert "#7" in prompt  # top-voted issue
    assert "Thumbs-up reactions: 12" in prompt


# ---------------------------------------------------------------------------
# Deterministic fallback (design.md sections 7 and 10)
# ---------------------------------------------------------------------------


def test_falls_back_to_heuristic_when_model_output_unparsable():
    report = analyst.analyze_repo(
        "owner/repo",
        context=_context(),
        run_model=lambda p: "I think this one is pretty easy, honestly.",
    )
    assert report.source == "heuristic"
    assert report.result is not None
    assert report.result.complexity in ("trivial", "moderate", "complex")
    assert "not parsable" in report.notes


def test_falls_back_to_heuristic_when_model_output_has_invalid_complexity():
    bad = json.dumps({"complexity": "very hard", "confidence": 0.9})
    report = analyst.analyze_repo("owner/repo", context=_context(), run_model=lambda p: bad)
    assert report.source == "heuristic"


def test_falls_back_to_heuristic_when_model_call_raises():
    def boom(prompt):
        raise RuntimeError("bedrock throttled")

    report = analyst.analyze_repo("owner/repo", context=_context(), run_model=boom)
    assert report.source == "heuristic"
    assert "model call failed" in report.notes
    assert report.result.complexity == "trivial"


def test_use_model_false_takes_the_deterministic_path():
    report = analyst.analyze_repo("owner/repo", context=_context(), use_model=False)
    assert report.source == "heuristic"
    assert report.result.complexity == "trivial"
    assert report.gate.proceed is True


def test_heuristic_path_needs_no_strands():
    # The whole point of the fallback: a verdict without the agent framework.
    report = analyst.analyze_repo("owner/repo", context=_context(), use_model=False)
    assert report.result is not None


# ---------------------------------------------------------------------------
# Gating (REQ-2.3) — returned, never persisted
# ---------------------------------------------------------------------------


def test_complex_verdict_recommends_skipped_complex():
    complex_json = json.dumps(
        {"complexity": "complex", "confidence": 0.95, "files_affected": [], "approach": "no"}
    )
    report = analyst.analyze_repo(
        "owner/repo", context=_context(), run_model=lambda p: complex_json
    )
    assert report.gate.proceed is False
    assert report.gate.status == SKIPPED_STATUS


def test_low_confidence_verdict_recommends_skipped_complex():
    low_json = json.dumps(
        {"complexity": "trivial", "confidence": 0.2, "files_affected": [], "approach": "maybe"}
    )
    report = analyst.analyze_repo("owner/repo", context=_context(), run_model=lambda p: low_json)
    assert report.gate.proceed is False
    assert report.gate.status == SKIPPED_STATUS


def test_threshold_override_is_honoured():
    report = analyst.analyze_repo(
        "owner/repo", context=_context(), run_model=lambda p: MODEL_JSON, threshold=0.95
    )
    assert report.result.confidence == 0.81
    assert report.gate.proceed is False


def test_repo_with_no_open_issues_is_skipped():
    report = analyst.analyze_repo("owner/repo", context=_context(issues=[]), use_model=False)
    assert report.source == "none"
    assert report.result is None
    assert report.issue_number is None
    assert report.gate.proceed is False
    assert report.gate.status == SKIPPED_STATUS


def test_analyst_does_not_write_dynamodb():
    # Boundary check (design.md section 3): the Analyst module must not touch
    # the state helpers at all. If it ever does, this fails loudly.
    import inspect

    source = inspect.getsource(analyst)
    assert "dynamo_tools" not in source
    assert "write_state" not in source


# ---------------------------------------------------------------------------
# Report serialization (agent-as-tool contract, design.md section 8)
# ---------------------------------------------------------------------------


def test_report_json_carries_the_design_verdict_and_the_gate():
    report = analyst.analyze_repo(
        "owner/repo", context=_context(), run_model=lambda p: MODEL_JSON
    )
    payload = json.loads(report.to_json())
    assert payload["repo_full_name"] == "owner/repo"
    assert payload["issue_number"] == 7
    assert set(payload["result"]) == {
        "complexity",
        "confidence",
        "files_affected",
        "approach",
    }
    assert payload["gate"]["proceed"] is True
    assert payload["source"] == "model"
    assert payload["notes"]


def test_analyst_agent_entry_point_returns_json(monkeypatch):
    if analyst.STRANDS_AVAILABLE:
        pytest.skip("wrapped by the strands @tool decorator; tested via analyze_repo")

    monkeypatch.setattr(
        analyst.github_tools, "gather_repo_context", lambda *a, **k: _context()
    )
    payload = json.loads(analyst.analyst_agent("owner/repo"))
    assert payload["repo_full_name"] == "owner/repo"
    assert payload["result"]["complexity"] in ("trivial", "moderate", "complex")
    assert "proceed" in payload["gate"]


# ---------------------------------------------------------------------------
# Prompt safety (NFR)
# ---------------------------------------------------------------------------


def test_system_prompt_uses_the_safe_maintenance_wording():
    prompt = analyst.ANALYST_SYSTEM_PROMPT
    assert "reduced maintenance activity" in prompt
    # It may instruct the model *not* to say dead/abandoned, but must not assert it.
    assert "repository is dead" not in prompt.lower()
    assert "Never describe a project or its maintainers as dead or abandoned." in prompt


def test_analysis_prompt_uses_the_safe_maintenance_wording():
    prompt = analyst.build_analysis_prompt(_context(), _issue())
    assert "appears to have reduced maintenance activity" in prompt


def test_system_prompt_documents_the_four_analysis_signals():
    prompt = analyst.ANALYST_SYSTEM_PROMPT.lower()
    assert "reproduction steps" in prompt
    assert "stack trace" in prompt
    assert "referenced files" in prompt
    assert "line numbers" in prompt
