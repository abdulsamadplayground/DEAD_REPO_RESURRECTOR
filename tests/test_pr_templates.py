"""Unit tests for src.tools.pr_templates (Communicator wording + reply triage).

Fully offline and dependency-free: these functions are just strings-to-strings
and a deterministic classifier, so there are no fakes, no network, no strands.

Covers REQ-4.1 (exact PR title), REQ-4.2 (four body sections in order with
``closes #{N}``), REQ-4.3 (the issue comment), REQ-5.3 (model-free reply
classification), and the github-conventions.md tone guard (never dead/abandoned/
unmaintained; always "appears to have reduced maintenance activity").
"""

from __future__ import annotations

import pytest

from src.tools import pr_templates as pt


# ---------------------------------------------------------------------------
# REQ-4.1 — PR title, EXACT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, number, expected",
    [
        (
            "Crash when parsing empty config",
            42,
            "[Resurrector] Fix: Crash when parsing empty config (closes #42)",
        ),
        (
            "Handle # in header names & <special> chars",
            7,
            "[Resurrector] Fix: Handle # in header names & <special> chars (closes #7)",
        ),
        (
            "  leading and trailing space  ",
            3,
            "[Resurrector] Fix: leading and trailing space (closes #3)",
        ),
    ],
)
def test_pr_title_is_exact(title, number, expected):
    assert pt.format_pr_title(title, number) == expected


def test_pr_title_blank_falls_back_to_issue_number():
    assert pt.format_pr_title("", 9) == "[Resurrector] Fix: issue #9 (closes #9)"
    assert pt.format_pr_title(None, 9) == "[Resurrector] Fix: issue #9 (closes #9)"


def test_pr_title_accepts_stringy_number():
    assert (
        pt.format_pr_title("Fix it", "12")
        == "[Resurrector] Fix: Fix it (closes #12)"
    )


def test_pr_title_trims_absurdly_long_title_but_keeps_prefix_and_suffix():
    long_title = "x" * 500
    result = pt.format_pr_title(long_title, 5)
    assert len(result) <= pt.MAX_PR_TITLE_LEN
    assert result.startswith("[Resurrector] Fix: ")
    assert result.endswith(" (closes #5)")
    assert result.endswith("… (closes #5)")  # trimmed with an ellipsis


def test_pr_title_rejects_bad_number():
    with pytest.raises(ValueError):
        pt.format_pr_title("t", 0)
    with pytest.raises(ValueError):
        pt.format_pr_title("t", -1)
    with pytest.raises(ValueError):
        pt.format_pr_title("t", "not-a-number")


# ---------------------------------------------------------------------------
# REQ-4.2 — PR body: four sections, in order, with closes #{N}
# ---------------------------------------------------------------------------


def test_pr_body_has_all_four_sections_in_order():
    body = pt.format_pr_body(
        issue_number=42,
        what_changed="Guard against an empty config file.",
        why="Empty configs crashed the loader.",
        how_to_test="Run `pytest tests/test_config.py`.",
    )
    positions = [body.index(h) for h in pt.PR_BODY_SECTION_HEADINGS]
    assert positions == sorted(positions), "sections must appear in order"
    # every heading present
    for heading in pt.PR_BODY_SECTION_HEADINGS:
        assert heading in body


def test_pr_body_references_issue_with_lowercase_closes():
    body = pt.format_pr_body(
        issue_number=42, what_changed="x", why="y", how_to_test="z"
    )
    assert "closes #42" in body  # lowercase, so GitHub auto-closes on merge


def test_pr_body_contains_the_fixed_co_maintenance_offer():
    body = pt.format_pr_body(
        issue_number=1, what_changed="x", why="y", how_to_test="z"
    )
    assert pt.REDUCED_MAINTENANCE_PHRASE in body
    assert "no obligation either way" in body


def test_pr_body_fills_placeholders_for_missing_substance():
    body = pt.format_pr_body(issue_number=1, what_changed="", why=None, how_to_test=None)
    # structure intact even when inputs are blank
    positions = [body.index(h) for h in pt.PR_BODY_SECTION_HEADINGS]
    assert positions == sorted(positions)
    assert "closes #1" in body


# ---------------------------------------------------------------------------
# REQ-4.3 — issue comment
# ---------------------------------------------------------------------------


def test_issue_comment_links_pr_and_invites_review():
    url = "https://github.com/o/r/pull/5"
    comment = pt.format_issue_comment(url)
    assert url in comment
    # 1-2 sentences: at most two sentence-terminating periods of prose
    assert comment.count(".") <= 3
    lowered = comment.lower()
    assert "feedback" in lowered or "review" in lowered or "adjust" in lowered


def test_follow_up_comment_is_one_warm_sentence():
    text = pt.follow_up_comment()
    assert "no rush" in text.lower()
    assert text.count(".") <= 1


# ---------------------------------------------------------------------------
# REQ-5.3 — deterministic, model-free reply classification
# ---------------------------------------------------------------------------


def test_merged_pr_classifies_as_merged():
    result = pt.classify_maintainer_reply(merged=True, state="closed", text="thanks")
    assert result.classification == "merged"
    assert result.signal == "pr_merged"


def test_closed_without_merge_classifies_as_closed():
    result = pt.classify_maintainer_reply(merged=False, state="closed", text="no thanks")
    assert result.classification == "closed"
    assert result.signal == "pr_closed_unmerged"


def test_structured_signal_wins_over_question_text():
    # A merged PR whose comment happens to contain a question is still "merged".
    result = pt.classify_maintainer_reply(
        merged=True, state="closed", text="why did this take so long?"
    )
    assert result.classification == "merged"


@pytest.mark.parametrize(
    "text",
    [
        "Why does this change the default?",
        "How do I run the tests for this?",
        "Could you explain the edge case here",
        "Is there a reason you skipped the cache?",
        "wondering if this handles unicode",
    ],
)
def test_question_text_classifies_as_question(text):
    result = pt.classify_maintainer_reply(text=text)
    assert result.classification == "question"
    assert result.signal == "text_question"


def test_plain_thanks_classifies_as_comment():
    result = pt.classify_maintainer_reply(text="thanks, looks great!")
    assert result.classification == "comment"
    assert result.signal == "text_comment"


def test_no_signal_classifies_as_other():
    assert pt.classify_maintainer_reply().classification == "other"
    assert pt.classify_maintainer_reply(text="   ").classification == "other"


def test_open_state_with_question_still_questions():
    # Structured state is "open" (not closed), so the text heuristic decides.
    result = pt.classify_maintainer_reply(state="open", text="what about windows?")
    assert result.classification == "question"


# ---------------------------------------------------------------------------
# Tone guard (github-conventions.md) — no banned words in any generated text
# ---------------------------------------------------------------------------


def test_no_generated_text_uses_a_banned_word():
    samples = [
        pt.format_pr_title("Fix the thing", 1),
        pt.format_pr_body(issue_number=1, what_changed="x", why="y", how_to_test="z"),
        pt.format_issue_comment("https://github.com/o/r/pull/1"),
        pt.follow_up_comment(),
        pt.CO_MAINTENANCE_NOTE,
    ]
    for text in samples:
        lowered = text.lower()
        for banned in pt.FORBIDDEN_TERMS:
            assert banned not in lowered, f"{banned!r} found in: {text!r}"


def test_reduced_maintenance_phrase_is_the_safe_wording():
    assert pt.REDUCED_MAINTENANCE_PHRASE in pt.CO_MAINTENANCE_NOTE
    for banned in pt.FORBIDDEN_TERMS:
        assert banned not in pt.REDUCED_MAINTENANCE_PHRASE
