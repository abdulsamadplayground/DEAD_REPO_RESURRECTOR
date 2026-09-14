"""Test-suite execution for the Engineer sub-agent (REQ-3.2) — the risky part.

REQ-3.2 requires that, *where tests exist*, the Engineer runs them and requires
they pass before proceeding. Satisfying that literally means executing a test
suite that was written by someone else, fetched from a repository we do not
control, chosen by a language model. That is arbitrary remote code execution
inside our own compute, with our own IAM role attached. It is the highest-risk
component in this system by a wide margin, and everything below is shaped by
that rather than by convenience.

Threat model
------------
The adversary is the *repository*, not the network. A repo that looks abandoned
is a cheap thing to plant: it costs nothing to publish plausible code with 40
open issues, wait for an automated contributor, and put a payload in
``conftest.py`` — which pytest imports before it runs a single test, with no
opt-in from us. The payload's goals would be, in order: read our GitHub PAT,
read our AWS credentials, and reach the network. So:

Controls
--------
1. **Execution is off by default.** ``RESURRECTOR_ALLOW_TEST_EXECUTION`` must be
   explicitly set to a truthy value (see :func:`execution_allowed`). With it
   unset, :func:`run_tests` returns :data:`OUTCOME_NOT_EXECUTED` — never
   :data:`OUTCOME_PASSED`. A skipped run is *not* a passing run, and
   :meth:`TestRunResult.passed` is the only thing that reports a pass. This is
   the single most effective control: the default posture executes nothing.
2. **No shell, ever.** :func:`subprocess.run` is called with an argument
   **list** and ``shell`` is never passed. No repository-derived string is ever
   interpolated into a command line, so there is nothing for ``;``, ``$( )``, or
   a newline to break out of.
3. **Command allowlist.** The command is derived from repository *markers* by
   :func:`detect_runner` and validated against :data:`ALLOWED_COMMANDS` by
   :func:`validate_command`. A caller (or a model) may propose a command, but it
   is checked against the same allowlist and rejected otherwise — we never
   execute a proposed command verbatim. Note the honest limit of this control:
   it stops us from being *told* to run ``curl | sh``; it does not stop
   ``pytest`` from importing a malicious ``conftest.py``. Only control 1 stops
   that.
4. **Scrubbed environment.** The child environment is built from an
   **allowlist** (:data:`ENV_ALLOW_KEYS`), not by filtering the parent. So
   ``GITHUB_TOKEN``, every ``AWS_*`` variable — including the
   ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` / ``AWS_SESSION_TOKEN`` pair that
   makes Lambda credentials trivially harvestable — and every
   ``RESURRECTOR_*`` setting are absent by construction rather than by
   enumeration. :func:`child_env` then re-asserts that none of
   :data:`ENV_DENY_KEYS` survived, as defence in depth against someone widening
   the allowlist later.
5. **Hard timeout** (``RESURRECTOR_TEST_TIMEOUT_SECONDS``, default 300s) on the
   subprocess, under the Engineer's own 10-minute deadline (NFR cost). A
   timeout is a **blocking** outcome, not a pass.
6. **Output caps.** stdout/stderr are truncated to
   ``RESURRECTOR_TEST_OUTPUT_BYTES`` (default 32 KiB) each, so a suite that
   prints a gigabyte cannot exhaust Lambda memory or the model's context.
7. **Contained working directory.** Execution happens under
   ``RESURRECTOR_WORK_DIR`` (default ``/tmp/resurrector``), which is also the
   only writable location in Lambda. ``HOME`` and ``TMPDIR`` are pointed there
   too, so the child cannot scribble in a real home directory.
   :func:`safe_join` does a ``realpath``-based containment check, which is the
   filesystem-level counterpart to the string-level
   :func:`src.tools.github_write.normalize_repo_path` — it catches a symlink
   inside the workspace pointing out of it, which no amount of path-string
   validation can.

Residual risk — stated plainly
------------------------------
These controls reduce the blast radius; they do not create a sandbox. With
execution enabled, a hostile suite still runs as our Lambda user, in our VPC,
with outbound network access, and can read anything on the filesystem that our
role can read. Scrubbing the environment removes the *easy* path to credentials;
in Lambda it does not remove the credential endpoint itself from the network.
Nothing here defends against a fork bomb, a filesystem filler within ``/tmp``,
or exfiltration over DNS.

The production answer is isolation, not filtering: run untrusted suites on
dedicated compute (a Fargate task or CodeBuild project) with **no** execution
role of consequence, egress denied by security group or a deny-all NAT, a
read-only root filesystem, and a hard task timeout — then report the result
back. That is a deployment change, out of scope for this task, which is why the
default here is "do not execute" rather than "execute carefully".

On ``strands.Agent(sandbox=...)``
---------------------------------
strands-agents 1.55.1 does accept a ``sandbox`` argument
(``strands.sandbox.base.Sandbox | None``), and it is more than a stub: the
package ships ``DockerSandbox`` and ``SshSandbox`` concrete backends alongside
the abstract ``PosixShellSandbox``. It is **not** usable as the control for this
module, for two reasons:

- The default is ``NotASandboxLocalEnvironment``, whose own docstring says it
  runs "directly on the host with **no isolation**" and is "not a security
  boundary". Passing no sandbox therefore buys nothing.
- A ``Sandbox`` governs code the *agent framework* executes through the sandbox
  API. Our :func:`run_tests` is a plain ``@tool`` calling
  :func:`subprocess.run` in-process, so it bypasses the sandbox entirely. To
  benefit, ``run_tests`` would have to be rewritten against the async
  ``Sandbox.execute`` interface.

``DockerSandbox`` needs a Docker daemon, which a Lambda execution environment
does not have; ``SshSandbox`` needs dedicated remote compute — which is exactly
the "production answer" above, arrived at from the other direction. Worth
revisiting if the Engineer ever moves off Lambda; recorded here so the next
person does not have to re-derive it. Not a blocker for this task.

Module name
-----------
Named ``suite_runner`` rather than ``test_runner`` on purpose: a module named
``test_*`` inside the source tree invites pytest to collect it as a test module.
``[tool.pytest.ini_options] testpaths = ["tests"]`` already scopes collection
away from ``src/``, but the name is one ``pytest src`` away from being a
problem, and an unambiguous name costs nothing.

Configuration
-------------
- ``RESURRECTOR_ALLOW_TEST_EXECUTION`` — master switch (default **off**)
- ``RESURRECTOR_TEST_TIMEOUT_SECONDS`` — per-suite timeout (default 300)
- ``RESURRECTOR_TEST_OUTPUT_BYTES`` — per-stream output cap (default 32768)
- ``RESURRECTOR_WORK_DIR`` — writable workspace root (default ``/tmp/resurrector``)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - execution is the point; see the module docstring
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ALLOW_EXECUTION_ENV_VAR = "RESURRECTOR_ALLOW_TEST_EXECUTION"
TEST_TIMEOUT_ENV_VAR = "RESURRECTOR_TEST_TIMEOUT_SECONDS"
OUTPUT_BYTES_ENV_VAR = "RESURRECTOR_TEST_OUTPUT_BYTES"
WORK_DIR_ENV_VAR = "RESURRECTOR_WORK_DIR"

DEFAULT_TEST_TIMEOUT_SECONDS = 300
DEFAULT_OUTPUT_BYTES = 32 * 1024
#: Lambda's filesystem is read-only apart from ``/tmp``, so any checkout has to
#: live there. Overridable for local runs and for the tests.
DEFAULT_WORK_DIR = "/tmp/resurrector"

#: Appended to a captured stream that hit the byte cap.
OUTPUT_TRUNCATION_MARKER = "\n...[truncated by resurrector at {limit} bytes]"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled"})


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` if unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var. Anything not clearly truthy is false."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def execution_allowed(override: Optional[bool] = None) -> bool:
    """Whether we are permitted to execute untrusted test code at all.

    ``override`` (an explicit argument from the caller) wins; otherwise
    ``RESURRECTOR_ALLOW_TEST_EXECUTION`` decides, and it defaults to **False**.
    Fail-closed: an unset, empty, or unrecognised value means "no".
    """
    if override is not None:
        return bool(override)
    return _env_flag(ALLOW_EXECUTION_ENV_VAR, False)


def test_timeout(timeout: Optional[int] = None) -> int:
    """Resolve the per-suite subprocess timeout in seconds (default 300).

    Public so the Engineer can clamp it against its own overall deadline without
    reaching into this module's internals.
    """
    if timeout is not None:
        return int(timeout)
    return _env_int(TEST_TIMEOUT_ENV_VAR, DEFAULT_TEST_TIMEOUT_SECONDS)


def output_cap(limit: Optional[int] = None) -> int:
    """Resolve the per-stream output cap in bytes (default 32768)."""
    if limit is not None:
        return int(limit)
    return _env_int(OUTPUT_BYTES_ENV_VAR, DEFAULT_OUTPUT_BYTES)


def work_dir() -> Path:
    """The writable root for checkouts (``/tmp`` on Lambda)."""
    return Path(os.environ.get(WORK_DIR_ENV_VAR) or DEFAULT_WORK_DIR)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NO_TESTS = "no_tests_found"
OUTCOME_NOT_EXECUTED = "not_executed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_ERROR = "error"

OUTCOMES = (
    OUTCOME_PASSED,
    OUTCOME_FAILED,
    OUTCOME_NO_TESTS,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_TIMED_OUT,
    OUTCOME_ERROR,
)

#: Outcomes that must stop the Engineer before it pushes (REQ-3.2 / REQ-3.3).
#:
#: - ``failed`` / ``timed_out`` — the suite told us the fix is not good enough.
#:   A suite that never finishes is a failing suite; REQ-3.2 requires tests to
#:   *pass*, and "still running" is not passing.
#: - ``error`` — we could not obtain a verdict (the runner binary is missing, the
#:   workspace is unusable). Treated as blocking because REQ-3.2's gate is
#:   "require they pass", and an unknown result does not clear it.
#:
#: Deliberately **not** blocking:
#:
#: - ``no_tests_found`` — REQ-3.2 is conditional ("WHERE tests exist"). A repo
#:   with no suite cannot fail one.
#: - ``not_executed`` — the operator switched execution off. Blocking here would
#:   make the safe default useless (nothing could ever ship); passing here would
#:   be a lie. It proceeds, and :attr:`TestRunResult.reason` says so out loud so
#:   the Communicator can be honest in the PR body.
BLOCKING_OUTCOMES = frozenset({OUTCOME_FAILED, OUTCOME_TIMED_OUT, OUTCOME_ERROR})


# ---------------------------------------------------------------------------
# Command allowlist
# ---------------------------------------------------------------------------

#: Command *prefixes* we are willing to execute, per ecosystem. A candidate
#: command must begin with one of these; anything else is refused. Note what is
#: absent: no ``make``, no ``tox``, no ``./scripts/test.sh``, no
#: ``npm install``. Those either run arbitrary repo-authored recipes (which
#: defeats the purpose of an allowlist) or fetch and execute dependencies from
#: the network.
ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("pytest",),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("npm", "test"),
    ("go", "test"),
    ("cargo", "test"),
)

#: Extra arguments are restricted to this shape. It admits flags, paths, and
#: ``k=v`` forms while excluding every shell metacharacter, whitespace, and
#: quote. Belt-and-braces: we never use a shell, so metacharacters would be
#: inert anyway, but a command that needs them is a command we did not intend.
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9._/@=:+-]+$")


class DisallowedCommandError(ValueError):
    """Raised when a proposed test command is not on the allowlist."""


def validate_command(argv: Sequence[str]) -> list[str]:
    """Validate a candidate test command against :data:`ALLOWED_COMMANDS`.

    Returns the command as a plain ``list[str]`` ready for
    :func:`subprocess.run`. Raises :class:`DisallowedCommandError` when the
    command does not start with an allowlisted prefix, when any element is not a
    string, or when a trailing argument contains anything outside
    :data:`_SAFE_ARG_RE` or a ``..`` path segment (which would let an argument
    point the runner outside the workspace).

    The traversal check is per **segment**, not a substring scan: ``../secrets``
    is refused, while Go's ``./...`` package pattern — which contains ``..`` as a
    substring but has no ``..`` segment — is allowed.

    This is the check applied to a command the *model* proposes as well as to
    one :func:`detect_runner` infers, so there is exactly one gate.
    """
    if not argv:
        raise DisallowedCommandError("empty command")
    if not all(isinstance(part, str) for part in argv):
        raise DisallowedCommandError("command must be a list of strings")

    parts = list(argv)
    for prefix in ALLOWED_COMMANDS:
        if tuple(parts[: len(prefix)]) == prefix:
            for extra in parts[len(prefix) :]:
                if not _SAFE_ARG_RE.match(extra):
                    raise DisallowedCommandError(
                        f"unsafe argument {extra!r} in {parts!r}"
                    )
                if any(segment == ".." for segment in extra.split("/")):
                    raise DisallowedCommandError(
                        f"argument {extra!r} may escape the workspace"
                    )
            return parts
    raise DisallowedCommandError(
        f"command {parts!r} is not on the allowlist "
        f"{[' '.join(p) for p in ALLOWED_COMMANDS]}"
    )


# ---------------------------------------------------------------------------
# Runner detection from repository markers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerSpec:
    """A recognised test runner: how to spot it and how to invoke it."""

    name: str
    command: tuple[str, ...]
    #: Files whose presence indicates this ecosystem.
    marker_files: tuple[str, ...] = ()
    #: Directories that usually hold the suite.
    marker_dirs: tuple[str, ...] = ()
    #: Glob patterns for actual test files, checked before we claim tests exist.
    test_globs: tuple[str, ...] = ()

    def command_list(self) -> list[str]:
        """The command as a mutable list."""
        return list(self.command)


#: Ordered most-specific first. ``pytest`` precedes ``unittest`` because a repo
#: with pytest config wants pytest, and pytest can run unittest-style tests
#: anyway.
KNOWN_RUNNERS: tuple[RunnerSpec, ...] = (
    RunnerSpec(
        name="pytest",
        command=("python", "-m", "pytest", "-q"),
        marker_files=("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"),
        marker_dirs=("tests", "test"),
        test_globs=("test_*.py", "*_test.py", "tests/**/test_*.py", "test/**/test_*.py"),
    ),
    RunnerSpec(
        name="unittest",
        command=("python", "-m", "unittest", "discover"),
        marker_files=("setup.py",),
        marker_dirs=("tests", "test"),
        test_globs=("test_*.py", "*_test.py", "tests/**/test_*.py", "test/**/test_*.py"),
    ),
    RunnerSpec(
        name="npm",
        command=("npm", "test"),
        marker_files=("package.json",),
        marker_dirs=("test", "tests", "__tests__", "spec"),
        test_globs=("**/*.test.js", "**/*.spec.js", "**/*.test.ts", "**/*.spec.ts"),
    ),
    RunnerSpec(
        name="go",
        command=("go", "test", "./..."),
        marker_files=("go.mod",),
        test_globs=("**/*_test.go",),
    ),
    RunnerSpec(
        name="cargo",
        command=("cargo", "test"),
        marker_files=("Cargo.toml",),
        marker_dirs=("tests",),
        test_globs=("**/*.rs",),
    ),
)

#: ``go test ./...`` is one token the arg regex would reject (``/`` is fine, but
#: the pattern must still be allowlisted as a whole) — keep the canonical
#: commands validated at import time so a typo here fails loudly and early,
#: rather than at the first live run.
for _spec in KNOWN_RUNNERS:
    validate_command(_spec.command)
del _spec


def _has_test_files(root: Path, spec: RunnerSpec) -> bool:
    """True when at least one file matching ``spec.test_globs`` exists.

    Marker files alone are not enough: a ``pyproject.toml`` proves the repo is
    Python, not that it has a suite. REQ-3.2 only gates on tests that actually
    exist, so we look for the files.
    """
    for pattern in spec.test_globs:
        try:
            if next(root.glob(pattern), None) is not None:
                return True
        except (OSError, ValueError):  # pragma: no cover - odd globs / perms
            continue
    for directory in spec.marker_dirs:
        candidate = root / directory
        if not candidate.is_dir():
            continue
        for pattern in spec.test_globs:
            bare = pattern.rsplit("/", 1)[-1]
            try:
                if next(candidate.rglob(bare), None) is not None:
                    return True
            except (OSError, ValueError):  # pragma: no cover
                continue
    return False


def detect_runner(root: Any) -> Optional[RunnerSpec]:
    """Infer the test runner for a checkout, or ``None`` when there is no suite.

    Requires **both** an ecosystem marker (``pyproject.toml``, ``go.mod``, ...)
    and at least one file that looks like a test. ``None`` is a legitimate,
    common answer — plenty of repos with reduced maintenance activity never had
    a suite — and it maps to :data:`OUTCOME_NO_TESTS`, which does not block.
    """
    base = Path(root)
    if not base.is_dir():
        return None
    for spec in KNOWN_RUNNERS:
        has_marker = any((base / name).exists() for name in spec.marker_files) or any(
            (base / name).is_dir() for name in spec.marker_dirs
        )
        if not has_marker:
            continue
        if not _has_test_files(base, spec):
            continue
        return spec
    return None


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------

#: The only variables an untrusted child is given, copied from our environment
#: when present. Everything else — the PAT, every AWS variable, our own
#: ``RESURRECTOR_*`` settings — is absent because it was never added.
ENV_ALLOW_KEYS: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "LD_LIBRARY_PATH",
    "SSL_CERT_FILE",
    "SYSTEMROOT",  # Windows-only; harmless elsewhere, needed for local dev runs
)

#: Never present in a child environment. The allowlist already guarantees this;
#: :func:`child_env` re-checks so that widening the allowlist later cannot
#: silently reintroduce a credential leak.
ENV_DENY_KEYS: frozenset[str] = frozenset(
    {
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_LAMBDA_RUNTIME_API",
    }
)

#: Prefixes stripped even if an allowlist entry ever matched them.
ENV_DENY_PREFIXES: tuple[str, ...] = ("AWS_", "RESURRECTOR_", "GITHUB_")


def child_env(cwd: Any, *, base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build the minimal environment for an untrusted test process.

    Allowlist-based, not filter-based: start from nothing, copy across only
    :data:`ENV_ALLOW_KEYS` that exist in ``base`` (default
    :data:`os.environ`), then add a few hardening variables.

    ``HOME`` and ``TMPDIR`` are pointed at the workspace so the child writes
    inside the directory we are about to delete rather than into a real home
    directory (or, on Lambda, into the read-only filesystem, where a suite that
    tries would fail for the wrong reason). ``PYTHONDONTWRITEBYTECODE`` keeps
    ``__pycache__`` out of the checkout. ``CI=1`` and ``PYTHONUNBUFFERED=1`` make
    runners non-interactive and their output ordered — a suite that blocks on a
    prompt would otherwise burn the whole timeout.
    """
    source = os.environ if base is None else base
    env: dict[str, str] = {}
    for key in ENV_ALLOW_KEYS:
        value = source.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", os.defpath)

    workspace = str(cwd)
    env["HOME"] = workspace
    env["TMPDIR"] = workspace
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["CI"] = "1"

    for key in list(env):
        if key in ENV_DENY_KEYS or key.startswith(ENV_DENY_PREFIXES):
            # Unreachable with the allowlist above; kept so a future widening of
            # ENV_ALLOW_KEYS cannot quietly leak a credential.
            del env[key]  # pragma: no cover
    return env


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------


def safe_join(root: Any, relative: str) -> Path:
    """Join ``relative`` onto ``root``, refusing anything that escapes it.

    Filesystem-level containment, which catches the two cases string validation
    cannot:

    - a directory in the path is a **symlink pointing out** of the workspace, so
      ``ws/link/x.py`` would really write to ``/etc/x.py``. The parent chain is
      resolved with ``realpath`` semantics and must land inside the workspace.
    - the destination itself is a symlink. Writing through it would redirect the
      write, so it is refused outright rather than followed. A repository whose
      fix genuinely needs to replace a symlink is rare enough that failing
      loudly beats guessing.

    ``root`` is resolved first, so a workspace under a symlinked ``/tmp``
    (macOS: ``/tmp`` → ``/private/tmp``) does not trip the check.

    Raises:
        ValueError: when the destination is a symlink or the resolved parent is
            not under the resolved root.
    """
    base = Path(root).resolve()
    raw = base / relative
    if raw.is_symlink():
        raise ValueError(
            f"{relative!r} is a symlink; refusing to write through it "
            f"(it escapes the workspace root {base})"
        )
    resolved_parent = raw.parent.resolve()
    if resolved_parent != base and base not in resolved_parent.parents:
        raise ValueError(f"{relative!r} escapes the workspace root {base}")
    return resolved_parent / raw.name


def create_workspace(prefix: str = "fix-") -> Path:
    """Create a fresh workspace directory under :func:`work_dir`.

    Uses :func:`tempfile.mkdtemp`, so the name is unguessable and the directory
    is created with mode 0700 — a concurrently-running function cannot read or
    plant files in it.
    """
    root = work_dir()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))


def materialize(root: Any, files: dict[str, str]) -> list[Path]:
    """Write ``{path: content}`` into the workspace, refusing path escapes.

    Every destination goes through :func:`safe_join`, which refuses a symlinked
    destination and a symlinked parent chain — writing through a symlink is a
    classic way to have a "checkout write" land somewhere else entirely. Parent
    directories are created as needed.
    """
    base = Path(root)
    written: list[Path] = []
    for relative, content in files.items():
        destination = safe_join(base, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append(destination)
    return written


def cleanup_workspace(root: Any) -> None:
    """Delete a workspace, best-effort.

    Lambda reuses ``/tmp`` across invocations in a warm container, so leaving a
    checkout behind would leak repository content — and disk — into the next
    repo's run. Failure to clean up is logged, never raised: it must not turn a
    successful fix into a failure.
    """
    try:
        shutil.rmtree(str(root), ignore_errors=False)
    except OSError:  # pragma: no cover - permissions / already gone
        LOGGER.warning("could not remove workspace %s", root, exc_info=True)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


def _cap(text: Optional[str], limit: int) -> tuple[str, bool]:
    """Truncate ``text`` to ``limit`` bytes, returning ``(text, truncated)``."""
    if not text:
        return "", False
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    return clipped + OUTPUT_TRUNCATION_MARKER.format(limit=limit), True


@dataclass
class TestRunResult:
    """The structured outcome of one test-suite attempt (REQ-3.2).

    Six distinct outcomes, because collapsing them is exactly how a "we didn't
    run anything" turns into a "tests passed" in a PR body:

    ``passed``          the suite ran and exited 0
    ``failed``          the suite ran and exited non-zero
    ``no_tests_found``  no suite exists — not a blocker (REQ-3.2 is conditional)
    ``not_executed``    execution is disabled — surfaced, never a pass
    ``timed_out``       the suite exceeded the timeout — blocking
    ``error``           we could not get a verdict — blocking
    """

    outcome: str
    reason: str = ""
    runner: Optional[str] = None
    command: Optional[list[str]] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: Optional[float] = None
    truncated: bool = False
    timeout_seconds: Optional[int] = None
    env_keys: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True **only** for a suite that actually ran and passed.

        The single source of truth for "tests are green". ``not_executed`` and
        ``no_tests_found`` are both false here by design.
        """
        return self.outcome == OUTCOME_PASSED

    @property
    def executed(self) -> bool:
        """True when a subprocess actually ran (pass, fail, or timeout)."""
        return self.outcome in (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_TIMED_OUT)

    def blocks_progress(self) -> bool:
        """Whether this result must stop the Engineer before pushing (REQ-3.2).

        See :data:`BLOCKING_OUTCOMES` for the reasoning behind each membership.
        """
        return self.outcome in BLOCKING_OUTCOMES

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "outcome": self.outcome,
            "passed": self.passed,
            "executed": self.executed,
            "blocking": self.blocks_progress(),
            "reason": self.reason,
            "runner": self.runner,
            "command": list(self.command) if self.command else None,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "truncated": self.truncated,
            "timeout_seconds": self.timeout_seconds,
        }


# ---------------------------------------------------------------------------
# run_tests (REQ-3.2)
# ---------------------------------------------------------------------------


def run_tests(
    root: Any,
    *,
    command: Optional[Sequence[str]] = None,
    allow_execution: Optional[bool] = None,
    timeout: Optional[int] = None,
    max_output_bytes: Optional[int] = None,
    runner_process: Optional[Callable[..., Any]] = None,
    env_base: Optional[dict[str, str]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> TestRunResult:
    """Run a repository's test suite under the controls in the module docstring.

    Order of operations, chosen so the cheap and safe checks come first:

    1. Is the workspace a directory? No → :data:`OUTCOME_ERROR`.
    2. Which runner do the markers imply, and are there test files at all?
       None → :data:`OUTCOME_NO_TESTS` (does not block).
    3. Validate the command against the allowlist — *before* consulting the
       master switch, so a bad command is reported as a bad command rather than
       hidden behind "execution disabled".
    4. Is execution permitted? No → :data:`OUTCOME_NOT_EXECUTED`. This is the
       default.
    5. Execute: argument list, no shell, scrubbed env, ``cwd`` in the workspace,
       hard timeout, capped output.

    ``command`` lets a caller (or the model, via the Engineer's tool) propose a
    command; it is validated against :data:`ALLOWED_COMMANDS` exactly like an
    inferred one and rejected if it does not match. ``runner_process`` is the
    injectable :func:`subprocess.run` seam — the tests pass a fake and assert on
    the arguments it receives, which is how "no ``shell=True``" and "no
    credentials in the child env" are verified rather than merely intended.

    This function does not raise for an unrunnable suite. Every failure mode maps
    to an outcome, because the Engineer's job is to make a decision (REQ-3.3),
    not to crash (design.md section 10).
    """
    timeout = test_timeout(timeout)
    max_output_bytes = output_cap(max_output_bytes)

    base = Path(root)
    if not base.is_dir():
        return TestRunResult(
            outcome=OUTCOME_ERROR,
            reason=f"workspace {base} is not a directory",
            timeout_seconds=timeout,
        )

    spec = detect_runner(base)
    if command is None:
        if spec is None:
            return TestRunResult(
                outcome=OUTCOME_NO_TESTS,
                reason=(
                    "no test suite detected from repository markers; REQ-3.2 only "
                    "requires tests to pass where tests exist, so this does not "
                    "block the fix"
                ),
                timeout_seconds=timeout,
            )
        argv = spec.command_list()
    else:
        argv = list(command)

    try:
        argv = validate_command(argv)
    except DisallowedCommandError as exc:
        return TestRunResult(
            outcome=OUTCOME_ERROR,
            reason=f"refusing to run a command outside the allowlist: {exc}",
            runner=spec.name if spec else None,
            timeout_seconds=timeout,
        )

    if not execution_allowed(allow_execution):
        return TestRunResult(
            outcome=OUTCOME_NOT_EXECUTED,
            reason=(
                "test execution is disabled; set "
                f"{ALLOW_EXECUTION_ENV_VAR}=true to allow running a third-party "
                "suite. This is NOT a passing result — no test was run."
            ),
            runner=spec.name if spec else None,
            command=argv,
            timeout_seconds=timeout,
        )

    env = child_env(base, base=env_base)
    runner_call = runner_process if runner_process is not None else subprocess.run
    started = clock()

    try:
        # No shell. Argument list only. Scrubbed env. Bounded time and output.
        completed = runner_call(
            argv,
            cwd=str(base),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, out_trunc = _cap(_as_text(getattr(exc, "stdout", None)), max_output_bytes)
        stderr, err_trunc = _cap(_as_text(getattr(exc, "stderr", None)), max_output_bytes)
        return TestRunResult(
            outcome=OUTCOME_TIMED_OUT,
            reason=(
                f"the suite did not finish within {timeout}s; treated as failing "
                "because REQ-3.2 requires tests to pass"
            ),
            runner=spec.name if spec else None,
            command=argv,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(clock() - started, 3),
            truncated=out_trunc or err_trunc,
            timeout_seconds=timeout,
            env_keys=sorted(env),
        )
    except (OSError, ValueError) as exc:
        # Missing runner binary, unexecutable cwd, bad argv shape.
        return TestRunResult(
            outcome=OUTCOME_ERROR,
            reason=f"could not start the test runner: {exc.__class__.__name__}: {exc}",
            runner=spec.name if spec else None,
            command=argv,
            duration_seconds=round(clock() - started, 3),
            timeout_seconds=timeout,
            env_keys=sorted(env),
        )

    duration = round(clock() - started, 3)
    exit_code = getattr(completed, "returncode", None)
    stdout, out_trunc = _cap(_as_text(getattr(completed, "stdout", None)), max_output_bytes)
    stderr, err_trunc = _cap(_as_text(getattr(completed, "stderr", None)), max_output_bytes)

    if exit_code == 0:
        outcome, reason = OUTCOME_PASSED, "the test suite passed"
    else:
        outcome, reason = (
            OUTCOME_FAILED,
            f"the test suite exited {exit_code}",
        )

    return TestRunResult(
        outcome=outcome,
        reason=reason,
        runner=spec.name if spec else None,
        command=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        truncated=out_trunc or err_trunc,
        timeout_seconds=timeout,
        env_keys=sorted(env),
    )


def _as_text(value: Any) -> str:
    """Coerce captured output to ``str`` (bytes happen when ``text`` is ignored)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
