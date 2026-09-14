"""Complexity scoring, model-output parsing, and the REQ-2.3 gate.

This module implements design.md section 7. It has three jobs:

1. :class:`ComplexityResult` — the structured verdict REQ-2.2 requires,
   ``{complexity, confidence, files_affected, approach}``.
2. :func:`parse_model_output` — tolerantly recover that JSON from whatever the
   model actually emitted (fenced blocks, surrounding prose, trailing chatter).
3. :func:`score_issue_complexity` — a **deterministic heuristic** verdict, used
   as the fallback when the model output is unparsable or Bedrock is
   unavailable (design.md sections 7 and 10).

Plus the REQ-2.3 gate: :func:`should_proceed` / :func:`gate_decision`.

Deliberately **pure standard library**. No ``strands``, no ``boto3``, no
``PyGithub``. That keeps the one piece of logic the whole Analyst falls back on
importable and testable in any environment.

Boundary note
-------------
REQ-2.3 says to "record ``status = skipped_complex``", but the Orchestrator is
the only component that writes DynamoDB state (design.md section 3). So this
module only ever **returns** the decision — :attr:`GateDecision.status` carries
the recommended status string for the Orchestrator (Task 8) to persist. Nothing
here touches DynamoDB.

Configuration
-------------
- ``RESURRECTOR_CONFIDENCE_THRESHOLD`` — gate threshold (default 0.6).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: The only three verdicts REQ-2.2 permits.
COMPLEXITY_LEVELS = ("trivial", "moderate", "complex")

#: Verdicts the pipeline is allowed to act on (REQ-2.4).
PROCEED_COMPLEXITIES = frozenset({"trivial", "moderate"})

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
CONFIDENCE_THRESHOLD_ENV_VAR = "RESURRECTOR_CONFIDENCE_THRESHOLD"

#: Recommended DynamoDB status when the gate refuses (REQ-2.3). Matches the
#: ``skipped_complex`` member of ``dynamo_tools.VALID_STATUSES``; declared as a
#: literal here so this module stays dependency-free.
SKIPPED_STATUS = "skipped_complex"

#: Most ``files_affected`` entries we will report, to bound prompt size.
MAX_FILES_AFFECTED = 10


def _env_float(name: str, default: float) -> float:
    """Read a float-valued env var, falling back to ``default`` if unset/invalid.

    Follows the ``_env_*`` convention used across the other modules.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def confidence_threshold(threshold: Optional[float] = None) -> float:
    """Resolve the gate threshold: explicit arg, then env var, then 0.6."""
    if threshold is not None:
        return float(threshold)
    return _env_float(CONFIDENCE_THRESHOLD_ENV_VAR, DEFAULT_CONFIDENCE_THRESHOLD)


# ---------------------------------------------------------------------------
# Result model (REQ-2.2)
# ---------------------------------------------------------------------------


@dataclass
class ComplexityResult:
    """The structured verdict for one issue (REQ-2.2, design.md section 7).

    Field order matches the design JSON exactly:
    ``{complexity, confidence, files_affected, approach}``.
    """

    complexity: str
    confidence: float
    files_affected: list[str]
    approach: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form, in the design's key order."""
        return {
            "complexity": self.complexity,
            "confidence": self.confidence,
            "files_affected": list(self.files_affected),
            "approach": self.approach,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Render as a JSON string (key order preserved, not sorted)."""
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ComplexityResult":
        """Build from a validated dict. Raises :class:`ValueError` if invalid."""
        validated = _validate_payload(payload)
        if validated is None:
            raise ValueError(f"invalid complexity payload: {payload!r}")
        return validated

    @classmethod
    def from_json(cls, text: str) -> "ComplexityResult":
        """Parse a JSON string. Raises :class:`ValueError` if invalid."""
        result = parse_model_output(text)
        if result is None:
            raise ValueError("could not parse a valid complexity result")
        return result


# ---------------------------------------------------------------------------
# Model-output parsing
# ---------------------------------------------------------------------------


def _iter_json_objects(text: str) -> Iterator[str]:
    """Yield every top-level ``{...}`` substring, outermost first.

    Brace-depth scan that is string- and escape-aware, so a ``}`` inside a JSON
    string literal does not close an object early. This is how we survive models
    that wrap their JSON in markdown fences or bracket it with prose.
    """
    depth = 0
    start: Optional[int] = None
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : index + 1]
                    start = None


def iter_json_objects(text: str) -> Iterator[str]:
    """Public handle on the brace-depth JSON scanner in :func:`_iter_json_objects`.

    Exposed because the Engineer (:mod:`src.agents.engineer`) has the same
    problem this solves — recovering a JSON object from model output that may be
    fenced or wrapped in prose — and one string-aware scanner is better than two.
    """
    return _iter_json_objects(text)


def _coerce_confidence(value: Any) -> Optional[float]:
    """Coerce ``value`` to a float in ``[0.0, 1.0]``, else ``None``.

    ``bool`` is rejected explicitly: ``True`` is a valid ``float()`` input in
    Python and would silently become a confidence of 1.0.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number < 0.0 or number > 1.0:
        return None
    return number


def _validate_payload(payload: Any) -> Optional[ComplexityResult]:
    """Validate a decoded JSON object into a :class:`ComplexityResult`.

    Rejects (returns ``None``) when: the payload is not an object,
    ``complexity`` is missing or outside :data:`COMPLEXITY_LEVELS`, or
    ``confidence`` is missing / non-numeric / outside ``[0.0, 1.0]``.

    Tolerates: ``complexity`` in any case with surrounding whitespace,
    ``confidence`` as a numeric string, ``files_affected`` given as a bare
    string instead of a list, and a missing ``files_affected`` / ``approach``.
    Those are cosmetic model slips, not wrong answers.
    """
    if not isinstance(payload, dict):
        return None

    raw_complexity = payload.get("complexity")
    if not isinstance(raw_complexity, str):
        return None
    complexity = raw_complexity.strip().lower()
    if complexity not in COMPLEXITY_LEVELS:
        return None

    confidence = _coerce_confidence(payload.get("confidence"))
    if confidence is None:
        return None

    raw_files = payload.get("files_affected", [])
    if raw_files is None:
        files: list[str] = []
    elif isinstance(raw_files, str):
        files = [raw_files.strip()] if raw_files.strip() else []
    elif isinstance(raw_files, (list, tuple)):
        files = [str(f).strip() for f in raw_files if str(f).strip()]
    else:
        return None

    raw_approach = payload.get("approach", "")
    if raw_approach is None:
        approach = ""
    elif isinstance(raw_approach, str):
        approach = raw_approach.strip()
    else:
        return None

    return ComplexityResult(
        complexity=complexity,
        confidence=confidence,
        files_affected=files[:MAX_FILES_AFFECTED],
        approach=approach,
    )


def parse_model_output(text: Any) -> Optional[ComplexityResult]:
    """Extract a :class:`ComplexityResult` from raw model text.

    Handles the three shapes models actually produce:

    - bare JSON,
    - JSON inside a ```` ```json ```` fence,
    - JSON surrounded by explanatory prose.

    Returns ``None`` — never raises — when nothing valid can be recovered, so
    the caller can fall back to :func:`score_issue_complexity` (design.md
    section 10). Candidate objects are tried outermost-first, so a result
    nested under a wrapper key is still found via the fallback pass below.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    for candidate in _iter_json_objects(text):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        result = _validate_payload(decoded)
        if result is not None:
            return result
        # The object parsed but wasn't a verdict; it may wrap one.
        if isinstance(decoded, dict):
            for value in decoded.values():
                nested = _validate_payload(value)
                if nested is not None:
                    return nested
    return None


# ---------------------------------------------------------------------------
# Signal detection for the deterministic heuristic
# ---------------------------------------------------------------------------

_STACK_TRACE_PATTERNS = (
    r"traceback \(most recent call last\)",
    r'^\s*file ".+", line \d+',
    r"^\s*at [\w$.<>\[\]]+\(",
    r"^\s*at [\w$.<>/]+:\d+",
    r"exception in thread",
    r"^panic:",
    r"goroutine \d+ \[",
    r"\bstack ?trace\b",
    r"^\s+at .+\.(java|kt|scala):\d+",
)

_ERROR_MESSAGE_PATTERNS = (
    r"\b\w*(?:error|exception)\s*:",
    r"^\s*(?:error|fatal|panic)\b\s*[:!]",
    r"\berrno\b",
    r"\bsegmentation fault\b",
    r"\bassertionerror\b",
    r"\bexit code [1-9]\d*\b",
)

_REPRO_PATTERNS = (
    r"steps? to reproduce",
    r"how to reproduce",
    r"\brepro(?:duction)?\s*(?:steps)?\s*:",
    r"^\s*1[.)]\s+\S",
    r"\bminimal (?:repro|reproducible|example)\b",
    r"\bexpected(?: behaviou?r)?\s*:",
)

_LINE_NUMBER_PATTERNS = (
    r"\bline\s+\d+",
    r"[\w./\-]+\.\w{1,6}:\d+",
    r'^\s*file ".+", line \d+',
    r"#l\d+",
    r"\blines?\s+\d+\s*[-–]\s*\d+",
)

_ARCHITECTURAL_KEYWORDS = (
    "rewrite",
    "from scratch",
    "redesign",
    "architecture",
    "architectural",
    "overhaul",
    "major refactor",
    "migrate to",
    "migration to",
    "breaking change",
    "backwards incompatible",
    "race condition",
    "deadlock",
    "memory leak",
    "thread safety",
    "thread-safety",
    "concurrency",
    "performance regression",
    "cross-platform support",
)

#: Labels that signal a small, well-scoped change.
EASY_LABELS = frozenset(
    {
        "good first issue",
        "good-first-issue",
        "goodfirstissue",
        "beginner",
        "beginner friendly",
        "beginner-friendly",
        "easy",
        "easy fix",
        "starter",
        "low hanging fruit",
        "documentation",
        "docs",
        "typo",
    }
)

#: Labels that signal a concrete defect: narrower than a feature request.
BUG_LABELS = frozenset({"bug", "defect", "regression", "crash"})

#: Labels that signal open-ended or wide-reaching work.
BROAD_LABELS = frozenset(
    {
        "enhancement",
        "feature",
        "feature request",
        "feature-request",
        "epic",
        "refactor",
        "refactoring",
        "rfc",
        "proposal",
        "design",
        "discussion",
        "question",
        "help wanted",
        "needs investigation",
        "needs design",
        "breaking change",
        "wontfix",
    }
)

#: File extensions we accept when extracting paths from free text. A whitelist
#: (rather than "anything with a dot") keeps version strings like ``3.11`` and
#: sentence fragments like ``e.g.`` out of ``files_affected``.
_PATH_EXTENSIONS = (
    "py|pyi|pyx|js|jsx|mjs|cjs|ts|tsx|vue|svelte|go|rs|java|kt|kts|rb|php|"
    "pl|c|h|cc|cpp|cxx|hpp|hh|cs|swift|scala|m|mm|sh|bash|zsh|fish|ps1|bat|"
    "yml|yaml|json|toml|cfg|ini|conf|properties|env|md|rst|adoc|txt|"
    "html|htm|css|scss|sass|less|sql|xml|gradle|lock|dockerfile|mk|cmake"
)

_PATH_RE = re.compile(
    r"(?<![\w.\-])((?:[\w.\-]+/)*[\w\-]+\.(?:" + _PATH_EXTENSIONS + r"))\b",
    re.IGNORECASE,
)

_BLOB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[\w.\-]+/[\w.\-]+/(?:blob|blame|tree)/[^/\s]+/([^\s?#)\]]+)",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_CODE_FENCE_RE = re.compile(r"```")


def extract_file_paths(text: Optional[str]) -> list[str]:
    """Pull plausible repository file paths out of free-form issue text.

    Two passes, because GitHub permalinks are the highest-signal way maintainers
    point at code:

    1. GitHub ``blob``/``blame``/``tree`` URLs contribute their in-repo path
       (any ``#L42`` anchor stripped).
    2. All URLs are then removed from the text and the remainder is scanned for
       bare paths whose extension is in the whitelist.

    Results are de-duplicated with order preserved and capped at
    :data:`MAX_FILES_AFFECTED`.
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        cleaned = candidate.strip().strip("`'\"(),;:").lstrip("./")
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        found.append(cleaned)

    for match in _BLOB_URL_RE.finditer(text):
        add(match.group(1).split("#")[0])

    for match in _PATH_RE.finditer(_URL_RE.sub(" ", text)):
        add(match.group(1))

    return found[:MAX_FILES_AFFECTED]


def _matches_any(patterns: Sequence[str], text: str) -> bool:
    """True when any pattern matches, multiline + case-insensitive."""
    return any(
        re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns
    )


def _normalize_labels(labels: Optional[Sequence[str]]) -> set[str]:
    """Lowercase / strip label names for set comparison."""
    return {str(label).strip().lower() for label in (labels or []) if str(label).strip()}


# ---------------------------------------------------------------------------
# Deterministic heuristic scorer (design.md sections 7 and 10)
# ---------------------------------------------------------------------------

#: Weight table for the heuristic. Kept as data, not buried in branches, so the
#: scoring is auditable and adjustable in one place.
POSITIVE_WEIGHTS = {
    "stack_trace": 2,
    "error_message": 1,
    "repro_steps": 2,
    "file_paths": 2,
    "line_numbers": 1,
    "code_block": 1,
    "easy_label": 2,
    "bug_label": 1,
}

NEGATIVE_WEIGHTS = {
    "no_body": 4,
    "short_body": 3,
    "sprawling_body": 1,
    "broad_label": 2,
    "many_files": 1,
    "architectural": 2,
}

#: Score at or above which an issue is ``trivial``.
TRIVIAL_SCORE = 5
#: Score at or above which an issue is ``moderate`` (below :data:`TRIVIAL_SCORE`).
MODERATE_SCORE = 2

#: Body shorter than this (and non-empty) is treated as too thin to plan from.
SHORT_BODY_CHARS = 40
#: Body longer than this is treated as sprawling.
SPRAWLING_BODY_CHARS = 4000
#: More implicated files than this is treated as a wide blast radius.
MANY_FILES = 5

#: Confidence starts here and moves with the evidence.
_CONFIDENCE_BASE = 0.5
_CONFIDENCE_PER_POSITIVE = 0.08
_CONFIDENCE_PER_NEGATIVE = 0.10
_CONFIDENCE_FLOOR = 0.05
_CONFIDENCE_CEILING = 0.95


def detect_signals(
    title: str = "",
    body: Optional[str] = None,
    *,
    labels: Optional[Sequence[str]] = None,
) -> dict[str, bool]:
    """Return the raw boolean signals the heuristic scores.

    Exposed separately from :func:`score_issue_complexity` so the detection and
    the weighting can be inspected (and tested) independently.
    """
    title = title or ""
    body_text = body or ""
    combined = f"{title}\n{body_text}"
    label_set = _normalize_labels(labels)
    paths = extract_file_paths(combined)

    body_stripped = body_text.strip()
    return {
        "stack_trace": _matches_any(_STACK_TRACE_PATTERNS, combined),
        "error_message": _matches_any(_ERROR_MESSAGE_PATTERNS, combined),
        "repro_steps": _matches_any(_REPRO_PATTERNS, combined),
        "file_paths": bool(paths),
        "line_numbers": _matches_any(_LINE_NUMBER_PATTERNS, combined),
        "code_block": bool(_CODE_FENCE_RE.search(body_text)),
        "easy_label": bool(label_set & EASY_LABELS),
        "bug_label": bool(label_set & BUG_LABELS),
        "no_body": not body_stripped,
        "short_body": bool(body_stripped) and len(body_stripped) < SHORT_BODY_CHARS,
        "sprawling_body": len(body_stripped) > SPRAWLING_BODY_CHARS,
        "broad_label": bool(label_set & BROAD_LABELS),
        "many_files": len(paths) > MANY_FILES,
        "architectural": any(
            keyword in combined.lower() for keyword in _ARCHITECTURAL_KEYWORDS
        ),
    }


def _build_approach(
    complexity: str, files: Sequence[str], signals: dict[str, bool]
) -> str:
    """Compose the short natural-language plan REQ-2.2 asks for.

    Wording follows the NFR safety rule: no claim that a repo is "dead", and no
    promise of a fix the evidence does not support.
    """
    file_hint = ", ".join(files[:3]) if files else "the file the issue points at"

    if complexity == "complex":
        reasons = []
        if signals["no_body"] or signals["short_body"]:
            reasons.append("the report has too little detail to plan from")
        if signals["architectural"]:
            reasons.append("it implies architectural or concurrency-level change")
        if signals["broad_label"]:
            reasons.append("its labels point at open-ended work")
        if signals["many_files"]:
            reasons.append("it implicates a wide set of files")
        if not reasons:
            reasons.append("the report lacks a reproducible failure to anchor a fix")
        return (
            "Do not attempt an automated fix: "
            + "; ".join(reasons)
            + ". Recommend asking the maintainers for reproduction steps and a "
            "narrower scope first."
        )

    steps = []
    if signals["repro_steps"]:
        steps.append("reproduce the failure using the steps in the issue")
    else:
        steps.append("reproduce the reported behaviour locally")
    if signals["stack_trace"] or signals["line_numbers"]:
        steps.append(f"follow the reported location into {file_hint}")
    else:
        steps.append(f"locate the responsible code, likely in {file_hint}")
    steps.append("apply a minimal, targeted change")
    steps.append("add or extend a regression test and run the existing suite")

    prefix = (
        "Small, well-localised change."
        if complexity == "trivial"
        else "Contained change with some investigation needed."
    )
    return prefix + " Plan: " + "; ".join(steps) + "."


def score_issue_complexity(
    title: str = "",
    body: Optional[str] = None,
    *,
    labels: Optional[Sequence[str]] = None,
    comments: int = 0,
) -> ComplexityResult:
    """Score an issue's complexity deterministically (no model involved).

    This is the fallback path required by design.md sections 7 and 10: it runs
    when the model output is unparsable or when Bedrock/``strands`` is
    unavailable. Same inputs always produce the same verdict.

    Scoring rules
    -------------
    Start at 0 and add the weight of every signal that fires.

    Evidence that a fix is *plannable* (:data:`POSITIVE_WEIGHTS`):

    ===================  ======  =====================================================
    signal               weight  fires when
    ===================  ======  =====================================================
    ``stack_trace``        +2    a traceback / stack frames / ``panic:`` is present
    ``error_message``      +1    an explicit ``SomeError:`` / ``fatal:`` / errno appears
    ``repro_steps``        +2    "steps to reproduce", a numbered list, "expected:"
    ``file_paths``         +2    at least one repository path is named
    ``line_numbers``       +1    ``line 42``, ``foo.py:42``, or a ``#L42`` anchor
    ``code_block``         +1    the body contains a fenced code block
    ``easy_label``         +2    labels include e.g. ``good first issue``, ``typo``
    ``bug_label``          +1    labels include e.g. ``bug``, ``regression``
    ===================  ======  =====================================================

    Evidence that a fix is *risky or unbounded* (:data:`NEGATIVE_WEIGHTS`):

    ====================  ======  ====================================================
    signal                weight  fires when
    ====================  ======  ====================================================
    ``no_body``             -4    the issue has no body at all
    ``short_body``          -3    the body is under 40 characters
    ``sprawling_body``      -1    the body exceeds 4000 characters
    ``broad_label``         -2    labels include e.g. ``enhancement``, ``rfc``
    ``many_files``          -1    more than 5 distinct files are implicated
    ``architectural``       -2    wording implies a rewrite, migration, or concurrency
    ====================  ======  ====================================================

    Verdict bands: ``score >= 5`` → ``trivial``; ``2 <= score <= 4`` →
    ``moderate``; ``score <= 1`` → ``complex``.

    Confidence is about *how much evidence we have*, not how easy the fix is:
    ``0.5 + 0.08 * (positive signals fired) - 0.10 * (negative signals fired)``,
    clamped to ``[0.05, 0.95]`` and rounded to two decimals. A one-line issue
    with no body therefore lands both in ``complex`` and below the default 0.6
    threshold, and the REQ-2.3 gate refuses it for two independent reasons.

    ``comments`` is accepted for interface symmetry with
    :class:`~src.tools.github_tools.IssueSummary` but is intentionally
    unweighted: comment volume on an abandoned repo signals frustration, not
    tractability, and pushed the scorer in the wrong direction in practice.
    """
    signals = detect_signals(title, body, labels=labels)
    files = extract_file_paths(f"{title or ''}\n{body or ''}")

    score = 0
    positives = 0
    negatives = 0
    for name, weight in POSITIVE_WEIGHTS.items():
        if signals[name]:
            score += weight
            positives += 1
    for name, weight in NEGATIVE_WEIGHTS.items():
        if signals[name]:
            score -= weight
            negatives += 1

    if score >= TRIVIAL_SCORE:
        complexity = "trivial"
    elif score >= MODERATE_SCORE:
        complexity = "moderate"
    else:
        complexity = "complex"

    confidence = (
        _CONFIDENCE_BASE
        + _CONFIDENCE_PER_POSITIVE * positives
        - _CONFIDENCE_PER_NEGATIVE * negatives
    )
    confidence = max(_CONFIDENCE_FLOOR, min(_CONFIDENCE_CEILING, confidence))

    return ComplexityResult(
        complexity=complexity,
        confidence=round(confidence, 2),
        files_affected=files,
        approach=_build_approach(complexity, files, signals),
    )


def score_issue(issue: Any) -> ComplexityResult:
    """Score a :class:`~src.tools.github_tools.IssueSummary`-shaped object.

    Duck-typed on ``title`` / ``body`` / ``labels`` / ``comments`` so this
    module keeps its zero-dependency property.
    """
    return score_issue_complexity(
        getattr(issue, "title", "") or "",
        getattr(issue, "body", None),
        labels=getattr(issue, "labels", None),
        comments=int(getattr(issue, "comments", 0) or 0),
    )


# ---------------------------------------------------------------------------
# REQ-2.3 gate
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    """The REQ-2.3 verdict: may the pipeline proceed to the Engineer?

    ``status`` is the DynamoDB status the **Orchestrator** should record — this
    module never writes it (design.md section 3). It is
    :data:`SKIPPED_STATUS` when the gate refuses and ``None`` when it allows,
    since a passing gate is not itself a state transition.
    """

    proceed: bool
    status: Optional[str]
    reason: str
    threshold: float
    complexity: Optional[str] = None
    confidence: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "proceed": self.proceed,
            "status": self.status,
            "reason": self.reason,
            "threshold": self.threshold,
            "complexity": self.complexity,
            "confidence": self.confidence,
        }


def gate_decision(
    result: Optional[ComplexityResult], *, threshold: Optional[float] = None
) -> GateDecision:
    """Apply the REQ-2.3 gate to a scored result.

    Proceed only when ``complexity in {trivial, moderate}`` **and**
    ``confidence >= threshold`` (default 0.6, override via
    ``RESURRECTOR_CONFIDENCE_THRESHOLD``). Otherwise refuse and recommend
    ``skipped_complex``.

    A ``None`` result (nothing could be scored — e.g. the repo has no open
    issues) also refuses: the safe default is to leave the repo alone.

    The threshold comparison is ``>=``, so a confidence exactly equal to the
    threshold proceeds. REQ-2.3 refuses when confidence is "below the
    configured threshold", and equal is not below.
    """
    resolved = confidence_threshold(threshold)

    if result is None:
        return GateDecision(
            proceed=False,
            status=SKIPPED_STATUS,
            reason="no complexity result was produced for this repo",
            threshold=resolved,
        )

    too_complex = result.complexity not in PROCEED_COMPLEXITIES
    low_confidence = result.confidence < resolved

    if too_complex or low_confidence:
        reasons = []
        if too_complex:
            reasons.append(f"complexity is {result.complexity!r}")
        if low_confidence:
            reasons.append(
                f"confidence {result.confidence} is below the threshold {resolved}"
            )
        return GateDecision(
            proceed=False,
            status=SKIPPED_STATUS,
            reason=" and ".join(reasons),
            threshold=resolved,
            complexity=result.complexity,
            confidence=result.confidence,
        )

    return GateDecision(
        proceed=True,
        status=None,
        reason=(
            f"complexity is {result.complexity!r} and confidence "
            f"{result.confidence} meets the threshold {resolved}"
        ),
        threshold=resolved,
        complexity=result.complexity,
        confidence=result.confidence,
    )


def should_proceed(
    result: Optional[ComplexityResult], *, threshold: Optional[float] = None
) -> bool:
    """Boolean shorthand for :func:`gate_decision` (REQ-2.3 / REQ-2.4)."""
    return gate_decision(result, threshold=threshold).proceed
