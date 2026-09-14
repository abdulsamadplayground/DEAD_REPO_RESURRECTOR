"""Unit tests for src.tools.complexity_scorer.

Pure-stdlib module, so these tests need no network, no AWS, and no ``strands``.

Covers:
- REQ-2.2: the exact ``{complexity, confidence, files_affected, approach}`` shape
  and the three permitted complexity values.
- Tolerant parsing of model output (fenced JSON, prose-wrapped JSON, invalid
  values, malformed input).
- The deterministic heuristic fallback (design.md sections 7 and 10).
- REQ-2.3 gating, including the configurable confidence threshold.
"""

from __future__ import annotations

import json

import pytest

import src.tools.complexity_scorer as cs

# ---------------------------------------------------------------------------
# Fixtures: representative issue text
# ---------------------------------------------------------------------------

TRIVIAL_TITLE = "TypeError in src/parser.py line 42 when parsing an empty string"

TRIVIAL_BODY = """\
### Steps to reproduce

1. Install version 1.2.0
2. Call `parse("")`

### Actual behaviour

```
Traceback (most recent call last):
  File "src/parser.py", line 42, in parse
    return tokens[0]
TypeError: 'NoneType' object is not subscriptable
```

### Expected behaviour

`parse("")` should return an empty list instead of raising.
"""

MODERATE_TITLE = "Crash when the config file is missing"

MODERATE_BODY = (
    "Starting the app without a config raises FileNotFoundError: config.yml. "
    "The lookup happens in config.py and there is no default fallback."
)

VAGUE_TITLE = "Doesn't work"


# ---------------------------------------------------------------------------
# ComplexityResult shape (REQ-2.2)
# ---------------------------------------------------------------------------


def test_result_to_dict_matches_design_json_shape_and_order():
    result = cs.ComplexityResult("trivial", 0.8, ["a/b.py"], "do the thing")
    payload = result.to_dict()
    assert list(payload.keys()) == [
        "complexity",
        "confidence",
        "files_affected",
        "approach",
    ]
    assert payload == {
        "complexity": "trivial",
        "confidence": 0.8,
        "files_affected": ["a/b.py"],
        "approach": "do the thing",
    }


def test_result_to_json_round_trips():
    result = cs.ComplexityResult("moderate", 0.65, ["x.py"], "plan")
    assert cs.ComplexityResult.from_json(result.to_json()) == result


def test_from_dict_rejects_invalid_payload():
    with pytest.raises(ValueError):
        cs.ComplexityResult.from_dict({"complexity": "impossible", "confidence": 0.9})


# ---------------------------------------------------------------------------
# parse_model_output
# ---------------------------------------------------------------------------


CLEAN = {
    "complexity": "moderate",
    "confidence": 0.72,
    "files_affected": ["src/a.py"],
    "approach": "patch the guard clause",
}


def test_parse_clean_json():
    result = cs.parse_model_output(json.dumps(CLEAN))
    assert result == cs.ComplexityResult("moderate", 0.72, ["src/a.py"], "patch the guard clause")


def test_parse_json_inside_markdown_fence():
    text = "```json\n" + json.dumps(CLEAN) + "\n```"
    assert cs.parse_model_output(text).complexity == "moderate"


def test_parse_json_surrounded_by_prose():
    text = (
        "I looked at the issue and the tree. Here is my assessment:\n\n"
        + json.dumps(CLEAN)
        + "\n\nHappy to expand on the plan if useful."
    )
    result = cs.parse_model_output(text)
    assert result.confidence == 0.72
    assert result.files_affected == ["src/a.py"]


def test_parse_json_nested_under_wrapper_key():
    text = json.dumps({"assessment": CLEAN, "meta": {"tokens": 12}})
    assert cs.parse_model_output(text).complexity == "moderate"


def test_parse_handles_braces_inside_strings():
    payload = dict(CLEAN, approach="replace {} with an empty dict literal")
    result = cs.parse_model_output("Result: " + json.dumps(payload))
    assert result.approach == "replace {} with an empty dict literal"


def test_parse_rejects_invalid_complexity_value():
    bad = dict(CLEAN, complexity="really hard")
    assert cs.parse_model_output(json.dumps(bad)) is None


def test_parse_rejects_confidence_above_one():
    assert cs.parse_model_output(json.dumps(dict(CLEAN, confidence=1.4))) is None


def test_parse_rejects_confidence_below_zero():
    assert cs.parse_model_output(json.dumps(dict(CLEAN, confidence=-0.1))) is None


def test_parse_rejects_boolean_confidence():
    # True would otherwise coerce to a confidence of 1.0.
    assert cs.parse_model_output(json.dumps(dict(CLEAN, confidence=True))) is None


def test_parse_rejects_missing_confidence():
    assert cs.parse_model_output(json.dumps({"complexity": "trivial"})) is None


def test_parse_returns_none_for_malformed_json():
    assert cs.parse_model_output("{not json at all") is None
    assert cs.parse_model_output("no braces here") is None
    assert cs.parse_model_output("") is None
    assert cs.parse_model_output(None) is None


def test_parse_normalizes_complexity_case_and_whitespace():
    assert cs.parse_model_output(json.dumps(dict(CLEAN, complexity=" Trivial "))).complexity == "trivial"


def test_parse_accepts_numeric_string_confidence():
    assert cs.parse_model_output(json.dumps(dict(CLEAN, confidence="0.5"))).confidence == 0.5


def test_parse_tolerates_missing_files_and_approach():
    result = cs.parse_model_output(json.dumps({"complexity": "complex", "confidence": 0.3}))
    assert result.files_affected == []
    assert result.approach == ""


def test_parse_accepts_files_affected_as_bare_string():
    result = cs.parse_model_output(json.dumps(dict(CLEAN, files_affected="src/only.py")))
    assert result.files_affected == ["src/only.py"]


# ---------------------------------------------------------------------------
# extract_file_paths
# ---------------------------------------------------------------------------


def test_extract_paths_finds_bare_and_nested_paths():
    text = "The bug is in src/pkg/parser.py and also setup.py"
    assert cs.extract_file_paths(text) == ["src/pkg/parser.py", "setup.py"]


def test_extract_paths_reads_github_blob_urls():
    text = "See https://github.com/o/r/blob/main/src/core/engine.py#L42 for the line"
    assert "src/core/engine.py" in cs.extract_file_paths(text)


def test_extract_paths_ignores_version_numbers_and_prose():
    text = "Requires Python 3.11, e.g. when using numpy 1.26 on macOS 14.2"
    assert cs.extract_file_paths(text) == []


def test_extract_paths_deduplicates_preserving_order():
    text = "config.py breaks, see config.py again, then utils.py"
    assert cs.extract_file_paths(text) == ["config.py", "utils.py"]


def test_extract_paths_caps_result_count():
    text = " ".join(f"file{i}.py" for i in range(30))
    assert len(cs.extract_file_paths(text)) == cs.MAX_FILES_AFFECTED


def test_extract_paths_handles_empty_input():
    assert cs.extract_file_paths(None) == []
    assert cs.extract_file_paths("") == []


# ---------------------------------------------------------------------------
# Deterministic heuristic scorer (design.md sections 7 and 10)
# ---------------------------------------------------------------------------


def test_well_specified_issue_scores_trivial_with_high_confidence():
    result = cs.score_issue_complexity(
        TRIVIAL_TITLE, TRIVIAL_BODY, labels=["bug", "good first issue"]
    )
    assert result.complexity == "trivial"
    assert result.confidence >= 0.6
    assert "src/parser.py" in result.files_affected
    assert result.approach


def test_moderately_specified_issue_scores_moderate_above_threshold():
    result = cs.score_issue_complexity(MODERATE_TITLE, MODERATE_BODY, labels=["bug"])
    assert result.complexity == "moderate"
    assert result.confidence >= cs.DEFAULT_CONFIDENCE_THRESHOLD


def test_vague_one_liner_scores_complex_with_low_confidence():
    result = cs.score_issue_complexity(VAGUE_TITLE, None)
    assert result.complexity == "complex"
    assert result.confidence < cs.DEFAULT_CONFIDENCE_THRESHOLD


def test_short_body_issue_scores_complex():
    result = cs.score_issue_complexity("Broken", "It crashes.")
    assert result.complexity == "complex"


def test_architectural_wording_pulls_score_down():
    with_arch = cs.score_issue_complexity(
        MODERATE_TITLE,
        MODERATE_BODY + " Fixing this properly needs a redesign of the loader.",
        labels=["bug"],
    )
    without_arch = cs.score_issue_complexity(MODERATE_TITLE, MODERATE_BODY, labels=["bug"])
    assert with_arch.confidence < without_arch.confidence


def test_broad_label_pulls_score_down():
    result = cs.score_issue_complexity(
        MODERATE_TITLE, MODERATE_BODY, labels=["enhancement", "rfc"]
    )
    assert result.complexity == "complex"


def test_scored_result_matches_design_json_shape_exactly():
    result = cs.score_issue_complexity(TRIVIAL_TITLE, TRIVIAL_BODY, labels=["bug"])
    payload = json.loads(result.to_json())
    assert set(payload) == {"complexity", "confidence", "files_affected", "approach"}
    assert payload["complexity"] in cs.COMPLEXITY_LEVELS
    assert isinstance(payload["confidence"], float)
    assert 0.0 <= payload["confidence"] <= 1.0
    assert isinstance(payload["files_affected"], list)
    assert all(isinstance(p, str) for p in payload["files_affected"])
    assert isinstance(payload["approach"], str) and payload["approach"]


def test_scorer_is_deterministic():
    first = cs.score_issue_complexity(TRIVIAL_TITLE, TRIVIAL_BODY, labels=["bug"])
    second = cs.score_issue_complexity(TRIVIAL_TITLE, TRIVIAL_BODY, labels=["bug"])
    assert first == second


def test_complex_approach_never_claims_the_repo_is_dead():
    # NFR Safety: the agent never calls a repo dead or abandoned.
    result = cs.score_issue_complexity(VAGUE_TITLE, None)
    lowered = result.approach.lower()
    assert "dead" not in lowered
    assert "abandoned" not in lowered


def test_detect_signals_reports_expected_flags():
    signals = cs.detect_signals(TRIVIAL_TITLE, TRIVIAL_BODY, labels=["good first issue"])
    assert signals["stack_trace"] is True
    assert signals["error_message"] is True
    assert signals["repro_steps"] is True
    assert signals["file_paths"] is True
    assert signals["line_numbers"] is True
    assert signals["code_block"] is True
    assert signals["easy_label"] is True
    assert signals["no_body"] is False


def test_score_issue_accepts_issue_summary_shape():
    from src.tools.github_tools import IssueSummary

    issue = IssueSummary(
        number=1,
        title=TRIVIAL_TITLE,
        body=TRIVIAL_BODY,
        thumbs_up=5,
        total_reactions=5,
        comments=2,
        labels=["bug"],
    )
    assert cs.score_issue(issue).complexity == "trivial"


# ---------------------------------------------------------------------------
# REQ-2.3 gating
# ---------------------------------------------------------------------------


def _result(complexity, confidence):
    return cs.ComplexityResult(complexity, confidence, [], "plan")


def test_gate_proceeds_for_trivial_above_threshold():
    decision = cs.gate_decision(_result("trivial", 0.9))
    assert decision.proceed is True
    assert decision.status is None
    assert cs.should_proceed(_result("trivial", 0.9)) is True


def test_gate_proceeds_for_moderate_above_threshold():
    assert cs.should_proceed(_result("moderate", 0.61)) is True


def test_gate_refuses_complex_even_with_high_confidence():
    decision = cs.gate_decision(_result("complex", 0.99))
    assert decision.proceed is False
    assert decision.status == cs.SKIPPED_STATUS
    assert "complex" in decision.reason


def test_gate_refuses_low_confidence():
    decision = cs.gate_decision(_result("trivial", 0.4))
    assert decision.proceed is False
    assert decision.status == cs.SKIPPED_STATUS
    assert "below the threshold" in decision.reason


def test_gate_confidence_exactly_at_threshold_proceeds():
    # REQ-2.3 refuses when confidence is *below* the threshold; equal is not below.
    assert cs.should_proceed(_result("moderate", 0.6), threshold=0.6) is True


def test_gate_threshold_is_configurable_via_env(monkeypatch):
    monkeypatch.setenv(cs.CONFIDENCE_THRESHOLD_ENV_VAR, "0.95")
    assert cs.should_proceed(_result("trivial", 0.8)) is False
    monkeypatch.setenv(cs.CONFIDENCE_THRESHOLD_ENV_VAR, "0.1")
    assert cs.should_proceed(_result("trivial", 0.2)) is True


def test_gate_threshold_argument_overrides_env(monkeypatch):
    monkeypatch.setenv(cs.CONFIDENCE_THRESHOLD_ENV_VAR, "0.95")
    assert cs.should_proceed(_result("trivial", 0.8), threshold=0.5) is True


def test_gate_ignores_invalid_env_threshold(monkeypatch):
    monkeypatch.setenv(cs.CONFIDENCE_THRESHOLD_ENV_VAR, "not-a-number")
    assert cs.confidence_threshold() == cs.DEFAULT_CONFIDENCE_THRESHOLD


def test_gate_refuses_when_there_is_no_result():
    decision = cs.gate_decision(None)
    assert decision.proceed is False
    assert decision.status == cs.SKIPPED_STATUS
    assert cs.should_proceed(None) is False


def test_gate_decision_reports_both_reasons():
    decision = cs.gate_decision(_result("complex", 0.2))
    assert "complex" in decision.reason and "below the threshold" in decision.reason


def test_gate_decision_is_json_serializable():
    payload = json.loads(json.dumps(cs.gate_decision(_result("trivial", 0.9)).to_dict()))
    assert payload["proceed"] is True
    assert payload["threshold"] == cs.DEFAULT_CONFIDENCE_THRESHOLD


def test_skipped_status_matches_the_state_machine():
    from src.tools.dynamo_tools import VALID_STATUSES

    assert cs.SKIPPED_STATUS in VALID_STATUSES
