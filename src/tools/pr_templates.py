"""Pure, dependency-free text for the Communicator (PR + comments + reply triage).

The Communicator is the only agent that opens PRs or posts comments (design.md
section 3). Everything a maintainer actually reads — the PR title, the PR body,
the issue comment, the follow-up nudge — is generated here, and every rule those
strings must obey lives in ``github-conventions.md``. Keeping the wording in one
zero-dependency module makes the conventions **auditable**: the fixed
co-maintenance note, the banned words, and the required "reduced maintenance
activity" phrasing are module-level constants a test can assert against, and the
PR-body section order is produced by one function rather than scattered through
the agent.

Why this is separate from :mod:`src.tools.github_comms`
------------------------------------------------------
Same reasoning the Engineer used to split :mod:`src.tools.github_write` from the
read module: the PyGithub write calls (``create_pull``, ``create_comment``) and
the pure string formatting have nothing in common at runtime, and separating
them means the templates can be exercised exhaustively with no network, no
``strands``, and no fake GitHub objects at all — they are just functions from
strings to strings. :mod:`src.tools.github_comms` imports *this* module for the
wording; this module imports nothing but the standard library.

What lives here
---------------
- :func:`format_pr_title` — REQ-4.1, EXACTLY ``[Resurrector] Fix: {title} (closes #{N})``
- :func:`format_pr_body` — REQ-4.2, the four fixed sections in order, ``closes #{N}``
- :func:`format_issue_comment` — REQ-4.3, the 1-2 sentence PR announcement
- :func:`follow_up_comment` — the single 7-day / 14-day nudge text (timing is the
  Orchestrator/Processor's job, Task 9 — this only supplies the wording)
- :func:`classify_maintainer_reply` — REQ-5.3, the **model-free** triage of a
  maintainer's activity into ``merged | question | closed | comment | other``

Tone rules (github-conventions.md), enforced by the constants below
-------------------------------------------------------------------
Never call a repository "dead", "abandoned", or "unmaintained"; always describe
it as one that "appears to have reduced maintenance activity". Every offer of
help is opt-in with an explicit no-obligation out. A test asserts none of the
generated strings contain a banned term.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Auditable tone constants (github-conventions.md)
# ---------------------------------------------------------------------------

#: Words the Communicator must never use about a repository (github-conventions.md).
#: Lower-cased; the tone-guard test checks generated text against these.
FORBIDDEN_TERMS = ("dead", "abandoned", "unmaintained")

#: The mandated safe phrasing that replaces every one of the banned words.
REDUCED_MAINTENANCE_PHRASE = "appears to have reduced maintenance activity"

#: The fixed "A note from the contributor" section (github-conventions.md). Its
#: wording is a constant, not something the model composes: it must keep the
#: "reduced maintenance activity" phrasing and an explicit no-obligation out.
#: Reflowed for line length, but semantically identical to the template.
CO_MAINTENANCE_NOTE = (
    "## A note from the contributor\n"
    "This project appears to have reduced maintenance activity recently. This fix "
    "was prepared to help. If it's useful and you'd welcome the help, I'm happy to "
    "assist with ongoing maintenance — no obligation either way."
)

#: REQ-4.1 fixes the prefix exactly. This is the requirement, not a knob.
PR_TITLE_PREFIX = "[Resurrector] Fix: "

#: GitHub caps a PR title at 256 characters. Only an absurdly long issue title
#: is ever trimmed; the prefix and the ``(closes #{N})`` suffix are always kept
#: intact so REQ-4.1's shape survives (see :func:`format_pr_title`).
MAX_PR_TITLE_LEN = 256

#: The four PR-body headings, in the REQ-4.2 / github-conventions.md order. Used
#: by the body builder and available to tests that assert the ordering.
PR_BODY_SECTION_HEADINGS = (
    "## What changed",
    "## Why",
    "## How to test",
    "## A note from the contributor",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_issue_number(issue_number: Any) -> int:
    """Return a positive int issue number, or raise.

    The number is interpolated into ``(closes #{N})`` and into ``closes #{N}`` in
    the body, both of which GitHub parses to auto-link/close the issue — so a
    bogus value must be rejected here rather than producing ``closes #None``.
    """
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            raise ValueError(f"issue_number must be an int, got {issue_number!r}")
    if issue_number <= 0:
        raise ValueError(f"issue_number must be positive, got {issue_number}")
    return issue_number


# ---------------------------------------------------------------------------
# PR title (REQ-4.1)
# ---------------------------------------------------------------------------


def format_pr_title(issue_title: Optional[str], issue_number: Any) -> str:
    """Return the REQ-4.1 PR title: ``[Resurrector] Fix: {title} (closes #{N})``.

    The prefix and the ``(closes #{N})`` suffix are constants; only the middle
    is variable. A missing/blank title falls back to ``issue #{N}`` so the shape
    still holds. An absurdly long title is trimmed (with an ellipsis) only as far
    as needed to keep the whole line within :data:`MAX_PR_TITLE_LEN`, and never
    at the cost of the prefix or the suffix.
    """
    number = _coerce_issue_number(issue_number)
    suffix = f" (closes #{number})"
    title = (issue_title or "").strip()
    if not title:
        title = f"issue #{number}"

    budget = MAX_PR_TITLE_LEN - len(PR_TITLE_PREFIX) - len(suffix)
    if budget >= 1 and len(title) > budget:
        title = title[: budget - 1].rstrip() + "…"
    return f"{PR_TITLE_PREFIX}{title}{suffix}"


# ---------------------------------------------------------------------------
# PR body (REQ-4.2)
# ---------------------------------------------------------------------------


def format_pr_body(
    *,
    issue_number: Any,
    what_changed: Optional[str],
    why: Optional[str] = None,
    how_to_test: Optional[str] = None,
) -> str:
    """Return the REQ-4.2 PR body: four fixed sections, in order, with ``closes #{N}``.

    Sections, in the github-conventions.md order:

    1. ``## What changed`` — the Engineer's summary of the code change.
    2. ``## Why`` — starts with ``closes #{N}.`` (lowercase, so GitHub auto-links
       and closes the issue on merge — REQ-4.2), then a one-line restatement of
       the issue in the maintainer's terms.
    3. ``## How to test`` — exact steps a maintainer can run.
    4. ``## A note from the contributor`` — the fixed :data:`CO_MAINTENANCE_NOTE`.

    Empty ``what_changed`` / ``how_to_test`` become explicit placeholders rather
    than blank sections, so the structure is always intact and never silently
    missing a heading.
    """
    number = _coerce_issue_number(issue_number)
    what = (what_changed or "").strip() or "_(no description provided)_"
    why_line = (why or "").strip()
    why_section = f"closes #{number}." + (f" {why_line}" if why_line else "")
    how = (how_to_test or "").strip() or "_(no test steps provided)_"

    return "\n".join(
        [
            "## What changed",
            what,
            "",
            "## Why",
            why_section,
            "",
            "## How to test",
            how,
            "",
            CO_MAINTENANCE_NOTE,
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Issue comment (REQ-4.3) and follow-up
# ---------------------------------------------------------------------------


def format_issue_comment(pr_url: str) -> str:
    """Return the REQ-4.3 issue comment: 1-2 sentences linking the PR.

    Announces that a PR addressing this issue is open, links it, and invites
    review — the example shape from github-conventions.md. Deliberately short and
    non-presumptuous; contains no banned term.
    """
    url = (pr_url or "").strip()
    return (
        f"Opened a PR that addresses this: {url}. "
        "Happy to adjust based on your feedback."
    )


def follow_up_comment(stage: int = 7) -> str:
    """Return the single, warm follow-up nudge (github-conventions.md).

    One sentence, no pressure. The *timing* of the 7-day and 14-day nudges (and
    the "never more than one of each" rule) is the Orchestrator/Processor's job
    (Task 9); this only supplies the wording. ``stage`` is accepted so a caller
    can distinguish the two nudges, but the copy is intentionally the same warm
    one-liner for both.
    """
    return "Still happy to help maintain this if it's useful — no rush."


# ---------------------------------------------------------------------------
# Reply classification (REQ-5.3) — deterministic, model-free
# ---------------------------------------------------------------------------

#: Interrogative openers / phrases that signal a maintainer is asking something.
#: Matched as whole words / phrases against lower-cased comment text.
_QUESTION_CUES = (
    "why",
    "how",
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "whose",
    "could you",
    "can you",
    "would you",
    "will you",
    "do you",
    "does this",
    "did you",
    "is there",
    "are there",
    "should i",
    "should we",
    "any chance",
    "wondering",
)


@dataclass
class ReplyClassification:
    """The triage of one maintainer interaction (REQ-5.3, design.md section 5).

    ``classification`` is one of ``merged | question | closed | comment | other``.
    ``confidence`` is how sure the deterministic rules are, and ``signal`` names
    what drove the decision — a structured GitHub flag (``pr_merged`` /
    ``pr_closed_unmerged``) or the text heuristic (``text_question`` /
    ``text_comment``) — so the Orchestrator (and a human reading a trace) can see
    *why*, not just *what*.
    """

    classification: str
    confidence: float
    signal: str
    matched: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "classification": self.classification,
            "confidence": self.confidence,
            "signal": self.signal,
            "matched": self.matched,
        }


def _text_has_question(text: str) -> Optional[str]:
    """Return the cue that makes ``text`` a question, or ``None``.

    A literal ``?`` is the strongest signal. Otherwise an interrogative opener /
    phrase at a word boundary counts. Returns the matched cue so the caller can
    record which one fired.
    """
    lowered = text.lower()
    if "?" in text:
        return "?"
    for cue in _QUESTION_CUES:
        if re.search(r"\b" + re.escape(cue) + r"\b", lowered):
            return cue
    return None


def classify_maintainer_reply(
    *,
    text: Optional[str] = None,
    merged: Optional[bool] = None,
    state: Optional[str] = None,
) -> ReplyClassification:
    """Classify a maintainer interaction without a model (REQ-5.3, design.md §10).

    Structured GitHub signals always win over the text heuristic, because a
    merge/close flag is ground truth and a comment's wording is a guess:

    1. ``merged is True`` → ``merged`` (the PR was merged; stop follow-up).
    2. ``state == "closed"`` and not merged → ``closed`` (closed without merging;
       REQ-5.4 → ``rejected``).
    3. otherwise fall back to ``text``: a question mark or an interrogative cue →
       ``question`` (REQ-5.3, the case that needs a helpful reply); any other
       non-empty text → ``comment``; nothing usable → ``other``.

    This is the deterministic fallback design.md section 10 requires: a model may
    later *compose* a nicer reply, but the *decision* of whether a reply is even
    needed never depends on one. Every branch is pure and offline-testable.
    """
    if merged is True:
        return ReplyClassification("merged", 1.0, "pr_merged")

    if isinstance(state, str) and state.strip().lower() == "closed" and merged is not True:
        return ReplyClassification("closed", 0.95, "pr_closed_unmerged")

    if text is not None and text.strip():
        cue = _text_has_question(text)
        if cue is not None:
            return ReplyClassification("question", 0.7, "text_question", matched=cue)
        return ReplyClassification("comment", 0.6, "text_comment")

    return ReplyClassification("other", 0.5, "none")
