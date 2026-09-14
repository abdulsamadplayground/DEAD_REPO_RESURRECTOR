"""Unit tests for src.agents.engineer.

Fully offline. No GitHub, no Bedrock, no AWS, and **no execution of third-party
test code**: the repository is a fake PyGithub object, the "model" is a plain
callable injected as ``run_model``, and the subprocess is a fake
``runner_process`` whose arguments we assert on.

``strands`` is installed in this project and is the core of the system, so the
wiring tests here actually build a real ``Agent`` rather than skipping.

Covers REQ-3.1 (branch), REQ-3.2 (tests gate), REQ-3.3 (abort with
``fix_failed``, no push), REQ-3.4 (push to the fix branch), the NFR deadline, and
the design.md section 3 boundaries.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import src.agents.engineer as engineer
from tests.test_github_write import FakeContentFile, FakeRepo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_PY = "def parse(text):\n    return text.split()\n"
BAD_PY = "def parse(text:\n    return text.split()\n"


def _repo(branch_exists=False, contents=None):
    """A fake repo with a default branch and, optionally, the fix branch."""
    return FakeRepo(
        default_branch="main",
        head_shas={"heads/main": "main-head"},
        existing_branches=("resurrector/fix-issue-7",) if branch_exists else (),
        contents=contents
        or {"src/parser.py": FakeContentFile(GOOD_PY.encode(), sha="old-blob")},
    )


def _model_reply(files=None, *, confident=True, message="Fix empty-input crash"):
    return json.dumps(
        {
            "summary": "guard the empty-token case",
            "commit_message": message,
            "files": files
            if files is not None
            else [{"path": "src/parser.py", "content": GOOD_PY}],
            "confident": confident,
        }
    )


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    """A fake ``subprocess.run`` that records and never executes."""

    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else Completed(0, "1 passed")
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def workspace_with_tests(tmp_path):
    """A checkout that detect_runner will recognise as a pytest repo."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_parser.py").write_text("def test_ok():\n    assert True\n")
    return tmp_path


@pytest.fixture
def no_test_execution(monkeypatch):
    monkeypatch.delenv("RESURRECTOR_ALLOW_TEST_EXECUTION", raising=False)


def _no_sleep(_seconds):
    return None


def _fix(repo, **kwargs):
    """implement_fix with the offline defaults every test wants."""
    kwargs.setdefault("issue_title", "TypeError on empty input")
    kwargs.setdefault("issue_body", "Calling parse('') raises.")
    kwargs.setdefault("approach", "guard the empty case")
    kwargs.setdefault("files_affected", ["src/parser.py"])
    kwargs.setdefault("sleep", _no_sleep)
    return engineer.implement_fix("owner/repo", 7, repo=repo, **kwargs)


# ---------------------------------------------------------------------------
# Strands wiring
# ---------------------------------------------------------------------------


def test_strands_is_available_in_this_project():
    # strands is the core of this system, not an optional add-on.
    assert engineer.STRANDS_AVAILABLE is True


def test_build_engineer_returns_a_real_agent():
    from strands import Agent

    agent = engineer.build_engineer()
    assert isinstance(agent, Agent)


def test_build_engineer_accepts_the_five_design_tools():
    from strands import Agent

    agent = Agent(tools=list(engineer.ENGINEER_TOOLS))
    assert agent is not None


def test_engineer_tool_set_has_the_five_design_tools():
    assert len(engineer.ENGINEER_TOOLS) == 5
    names = {
        getattr(t, "__name__", None) or getattr(t, "tool_name", None)
        for t in engineer.ENGINEER_TOOLS
    }
    for expected in (
        "create_branch",
        "get_file",
        "write_file",
        "run_tests",
        "push_commit",
    ):
        assert any(expected == n or (n and expected in str(n)) for n in names), expected


def test_build_engineer_uses_the_engineer_system_prompt():
    agent = engineer.build_engineer()
    assert engineer.ENGINEER_SYSTEM_PROMPT in str(agent.system_prompt)


# ---------------------------------------------------------------------------
# Model output parsing
# ---------------------------------------------------------------------------


def test_parse_fix_plan_reads_bare_json():
    plan = engineer.parse_fix_plan(_model_reply())
    assert plan is not None
    assert plan.paths() == ["src/parser.py"]
    assert plan.commit_message == "Fix empty-input crash"
    assert plan.confident is True


def test_parse_fix_plan_reads_json_in_a_fence_with_prose():
    noisy = "Here you go:\n```json\n" + _model_reply() + "\n```\nHope that helps."
    plan = engineer.parse_fix_plan(noisy)
    assert plan is not None and plan.paths() == ["src/parser.py"]


def test_parse_fix_plan_rejects_a_traversal_path():
    reply = _model_reply(files=[{"path": "../../etc/passwd", "content": "pwned"}])
    assert engineer.parse_fix_plan(reply) is None


def test_parse_fix_plan_rejects_prose_with_no_json():
    assert engineer.parse_fix_plan("I could not figure this one out.") is None


def test_parse_fix_plan_rejects_an_empty_files_array():
    assert engineer.parse_fix_plan(json.dumps({"files": []})) is None


def test_parse_fix_plan_carries_confident_false():
    plan = engineer.parse_fix_plan(_model_reply(confident=False))
    assert plan is not None and plan.confident is False


# ---------------------------------------------------------------------------
# REQ-3.1 — the branch
# ---------------------------------------------------------------------------


def test_branch_is_named_per_req_3_1_and_cut_from_the_default_branch(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: _model_reply())

    assert result.success is True
    assert result.branch == "resurrector/fix-issue-7"
    assert result.base_branch == "main"
    assert result.branch_created is True
    assert repo.created_refs == [
        ("refs/heads/resurrector/fix-issue-7", "main-head")
    ]


def test_an_existing_fix_branch_is_reused(no_test_execution):
    repo = _repo(branch_exists=True)
    result = _fix(repo, run_model=lambda p: _model_reply())

    assert result.success is True
    assert result.branch_created is False
    assert repo.created_refs == []


# ---------------------------------------------------------------------------
# REQ-3.4 — the push
# ---------------------------------------------------------------------------


def test_the_commit_is_pushed_to_the_fix_branch(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: _model_reply())

    assert result.success is True
    assert result.commit_shas == ["pushed-commit"]
    assert result.files_changed == ["src/parser.py"]
    # One tree, one commit, one ref move — onto the fix branch, not main.
    assert len(repo.created_commits) == 1
    assert repo.created_commits[0]["message"] == "Fix empty-input crash"
    assert repo.refs_handed_out["heads/resurrector/fix-issue-7"].edits == [
        "pushed-commit"
    ]
    assert "heads/main" not in repo.refs_handed_out or not repo.refs_handed_out[
        "heads/main"
    ].edits


def test_a_multi_file_fix_lands_as_one_commit(no_test_execution):
    repo = _repo()
    reply = _model_reply(
        files=[
            {"path": "src/parser.py", "content": GOOD_PY},
            {"path": "tests/test_parser.py", "content": "def test_x():\n    pass\n"},
        ]
    )
    result = _fix(repo, run_model=lambda p: reply)

    assert result.success is True
    assert sorted(result.files_changed) == ["src/parser.py", "tests/test_parser.py"]
    assert len(repo.created_commits) == 1  # atomic, not one commit per file
    assert len(repo.created_trees) == 1
    assert repo.created_files == []  # the Contents API was not used
    assert repo.updated_files == []


def test_a_caller_supplied_change_set_skips_the_model(no_test_execution):
    repo = _repo()
    result = engineer.implement_fix(
        "owner/repo",
        7,
        changes=[{"path": "src/parser.py", "content": GOOD_PY}],
        commit_message="Fix it",
        repo=repo,
        use_model=False,
        sleep=_no_sleep,
    )
    assert result.success is True
    assert result.source == "caller"
    assert repo.created_commits[0]["message"] == "Fix it"


# ---------------------------------------------------------------------------
# REQ-3.3 — invalid syntax aborts before any push
# ---------------------------------------------------------------------------


def test_invalid_python_aborts_with_fix_failed_and_never_pushes(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: _model_reply(
        files=[{"path": "src/parser.py", "content": BAD_PY}]
    ))

    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert "not syntactically valid" in result.reason
    assert "SyntaxError" in result.reason

    # Nothing was pushed, by any route.
    assert result.commit_shas == []
    assert repo.created_commits == []
    assert repo.created_trees == []
    assert repo.created_files == []
    assert repo.updated_files == []
    # ...and no stray branch was left behind either.
    assert repo.created_refs == []
    assert result.branch is None

    # The failing check is reported for the Orchestrator's notes.
    failed = [c for c in result.syntax_checks if not c["ok"]]
    assert failed and failed[0]["path"] == "src/parser.py"


def test_invalid_json_also_aborts(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: _model_reply(
        files=[{"path": "config.json", "content": "{broken,}"}]
    ))
    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS


def test_an_unparsable_model_reply_aborts_with_fix_failed(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: "I'm not sure how to fix this.")

    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert "could not obtain a usable change set" in result.reason
    assert repo.created_refs == []
    assert repo.created_commits == []


def test_a_model_that_reports_low_confidence_aborts(no_test_execution):
    repo = _repo()
    result = _fix(repo, run_model=lambda p: _model_reply(confident=False))

    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert "confident=false" in result.reason
    assert repo.created_commits == []


def test_a_model_call_that_raises_aborts_without_crashing(no_test_execution):
    def boom(prompt):
        raise RuntimeError("bedrock throttled")

    result = _fix(_repo(), run_model=boom)
    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS


def test_no_model_backend_aborts_cleanly(no_test_execution):
    result = _fix(_repo(), use_model=False)
    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS


def test_a_github_error_becomes_a_failure_result_not_an_exception(no_test_execution):
    from github.GithubException import GithubException

    repo = FakeRepo(
        default_branch="main",
        create_ref_error=GithubException(500, {"message": "boom"}, {}),
    )
    result = _fix(repo, run_model=lambda p: _model_reply())
    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert "GithubException" in result.reason


def test_a_rejected_caller_change_set_aborts(no_test_execution):
    result = engineer.implement_fix(
        "owner/repo",
        7,
        changes=[{"path": "../escape.py", "content": "x"}],
        repo=_repo(),
        use_model=False,
        sleep=_no_sleep,
    )
    assert result.success is False
    assert "rejected" in result.reason
    assert result.recommended_status == engineer.FIX_FAILED_STATUS


def test_a_bad_issue_number_aborts():
    result = engineer.implement_fix("owner/repo", "not-a-number", use_model=False)
    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS


# ---------------------------------------------------------------------------
# REQ-3.2 — the tests gate
# ---------------------------------------------------------------------------


def test_tests_exist_and_pass_so_the_fix_proceeds(
    monkeypatch, workspace_with_tests
):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    repo = _repo()
    runner = RecordingRunner(Completed(0, "1 passed"))

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
    )

    assert result.success is True
    assert result.test_result["outcome"] == "passed"
    assert result.test_result["passed"] is True
    assert result.commit_shas == ["pushed-commit"]
    # The change set was materialized into the workspace before the run.
    assert (workspace_with_tests / "src/parser.py").read_text() == GOOD_PY


def test_tests_exist_and_fail_so_the_fix_aborts_without_pushing(
    monkeypatch, workspace_with_tests
):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    repo = _repo()
    runner = RecordingRunner(Completed(1, "1 failed", "assert False"))

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
    )

    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert result.test_result["outcome"] == "failed"
    assert "tests did not pass" in result.reason
    # REQ-3.3: no push, no PR. The branch exists (REQ-3.1 ran) but is untouched.
    assert repo.created_commits == []
    assert repo.created_trees == []
    assert result.commit_shas == []
    assert result.branch == "resurrector/fix-issue-7"


def test_a_test_timeout_aborts_the_fix(monkeypatch, workspace_with_tests):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    repo = _repo()
    runner = RecordingRunner(
        raises=subprocess.TimeoutExpired(cmd=["pytest"], timeout=1)
    )

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
    )

    assert result.success is False
    assert result.test_result["outcome"] == "timed_out"
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert repo.created_commits == []


def test_no_tests_found_does_not_block_the_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    (tmp_path / "app.py").write_text("x = 1\n")
    repo = _repo()
    runner = RecordingRunner()

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        workspace=tmp_path,
        runner_process=runner,
    )

    assert result.success is True
    assert result.test_result["outcome"] == "no_tests_found"
    assert result.test_result["passed"] is False  # not a pass, just not a blocker
    assert result.commit_shas == ["pushed-commit"]
    assert runner.calls == []  # nothing was executed


def test_disabled_execution_is_surfaced_and_not_counted_as_a_pass(
    no_test_execution, workspace_with_tests
):
    repo = _repo()
    runner = RecordingRunner()

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
    )

    assert result.success is True  # the safe default must not block every fix
    assert result.test_result["outcome"] == "not_executed"
    assert result.test_result["passed"] is False
    assert result.test_result["executed"] is False
    # Surfaced honestly, so the Communicator cannot claim the suite is green.
    assert "not_executed" in result.notes
    assert runner.calls == []


def test_execution_can_be_forced_off_per_call(workspace_with_tests, monkeypatch):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    runner = RecordingRunner()
    result = _fix(
        _repo(),
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
        allow_test_execution=False,
    )
    assert result.test_result["outcome"] == "not_executed"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Security — what the subprocess is actually handed
# ---------------------------------------------------------------------------


def test_the_test_subprocess_never_gets_a_shell_or_our_credentials(
    monkeypatch, workspace_with_tests
):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
    runner = RecordingRunner()

    _fix(
        _repo(),
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
    )

    call = runner.calls[0]
    assert "shell" not in call["kwargs"]
    assert isinstance(call["argv"], list)

    env = call["kwargs"]["env"]
    assert "GITHUB_TOKEN" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "ghp_supersecret" not in "".join(env.values())

    # The command came from the allowlist.
    from src.tools.suite_runner import ALLOWED_COMMANDS

    argv = call["argv"]
    assert any(tuple(argv[: len(p)]) == p for p in ALLOWED_COMMANDS)


def test_the_suite_timeout_is_clamped_to_the_remaining_deadline(
    monkeypatch, workspace_with_tests
):
    monkeypatch.setenv("RESURRECTOR_ALLOW_TEST_EXECUTION", "true")
    runner = RecordingRunner()
    # A 20s overall deadline must win over the 300s default suite timeout.
    _fix(
        _repo(),
        run_model=lambda p: _model_reply(),
        workspace=workspace_with_tests,
        runner_process=runner,
        timeout=20,
    )
    assert runner.calls[0]["kwargs"]["timeout"] <= 20


# ---------------------------------------------------------------------------
# NFR cost — the 10-minute deadline
# ---------------------------------------------------------------------------


def test_the_default_deadline_is_ten_minutes(monkeypatch):
    monkeypatch.delenv(engineer.ENGINEER_TIMEOUT_ENV_VAR, raising=False)
    assert engineer.engineer_timeout() == 600


def test_the_deadline_is_configurable(monkeypatch):
    monkeypatch.setenv(engineer.ENGINEER_TIMEOUT_ENV_VAR, "120")
    assert engineer.engineer_timeout() == 120
    monkeypatch.setenv(engineer.ENGINEER_TIMEOUT_ENV_VAR, "nonsense")
    assert engineer.engineer_timeout() == 600


def test_an_exceeded_deadline_aborts_before_the_push(no_test_execution):
    repo = _repo()
    ticks = iter([0.0, 0.0, 5000.0, 5000.0, 5000.0, 5000.0])

    def fake_now():
        try:
            return next(ticks)
        except StopIteration:
            return 5000.0

    result = _fix(
        repo,
        run_model=lambda p: _model_reply(),
        timeout=600,
        now=fake_now,
    )

    assert result.success is False
    assert result.recommended_status == engineer.FIX_FAILED_STATUS
    assert "deadline" in result.reason
    assert repo.created_commits == []
    assert result.commit_shas == []


# ---------------------------------------------------------------------------
# Result serialization (agent-as-tool contract, design.md section 8)
# ---------------------------------------------------------------------------


def test_result_json_carries_everything_the_orchestrator_needs(no_test_execution):
    result = _fix(_repo(), run_model=lambda p: _model_reply())
    payload = json.loads(result.to_json())

    assert payload["repo_full_name"] == "owner/repo"
    assert payload["issue_number"] == 7
    assert payload["success"] is True
    assert payload["branch"] == "resurrector/fix-issue-7"
    assert payload["commit_shas"] == ["pushed-commit"]
    assert payload["files_changed"] == ["src/parser.py"]
    assert payload["recommended_status"] is None
    # No workspace was supplied, so the Engineer made a scratch one holding only
    # the changed file — there is no suite in it to find.
    assert payload["test_result"]["outcome"] == "no_tests_found"


def test_failure_json_carries_the_recommended_status(no_test_execution):
    result = _fix(
        _repo(),
        run_model=lambda p: _model_reply(
            files=[{"path": "src/parser.py", "content": BAD_PY}]
        ),
    )
    payload = json.loads(result.to_json())
    assert payload["success"] is False
    assert payload["recommended_status"] == "fix_failed"


def test_the_result_is_json_serializable_in_every_branch(no_test_execution):
    for reply in (_model_reply(), "garbage", _model_reply(confident=False)):
        result = _fix(_repo(), run_model=lambda p, r=reply: r)
        json.loads(result.to_json())


# ---------------------------------------------------------------------------
# Prompt construction and safety (NFR)
# ---------------------------------------------------------------------------


def test_the_prompt_carries_the_issue_and_the_real_file_contents():
    captured = {}

    def fake_model(prompt):
        captured["prompt"] = prompt
        return _model_reply()

    _fix(_repo(), run_model=fake_model)
    prompt = captured["prompt"]
    assert "owner/repo" in prompt
    assert "#7" in prompt
    assert "TypeError on empty input" in prompt
    assert "guard the empty case" in prompt
    assert "src/parser.py" in prompt
    assert "def parse(text):" in prompt  # the file was actually read
    assert "resurrector/fix-issue-7" in prompt


def test_a_file_the_analyst_invented_does_not_abort_the_run():
    captured = {}

    def fake_model(prompt):
        captured["prompt"] = prompt
        return _model_reply()

    result = _fix(
        _repo(),
        run_model=fake_model,
        files_affected=["src/parser.py", "src/does_not_exist.py"],
    )
    assert result.success is True
    assert "does not exist yet" in captured["prompt"]


def test_system_prompt_uses_the_safe_maintenance_wording():
    prompt = engineer.ENGINEER_SYSTEM_PROMPT
    assert "reduced maintenance activity" in prompt
    assert "repository is dead" not in prompt.lower()
    assert "Never describe a project or its maintainers as dead or abandoned." in prompt


def test_fix_prompt_uses_the_safe_maintenance_wording():
    prompt = engineer.build_fix_prompt(
        repo_full_name="owner/repo",
        issue_number=7,
        issue_title="t",
        issue_body="b",
        approach="a",
        branch="resurrector/fix-issue-7",
        files=[],
    )
    assert "appears to have reduced maintenance activity" in prompt


def test_system_prompt_forbids_prs_comments_and_ci_edits():
    prompt = engineer.ENGINEER_SYSTEM_PROMPT
    assert "do NOT open pull requests" in prompt
    assert "do NOT post comments" in prompt
    assert "Never modify CI configuration or workflow files." in prompt


def test_system_prompt_demands_minimal_change():
    prompt = engineer.ENGINEER_SYSTEM_PROMPT.lower()
    assert "change as little as possible" in prompt
    assert "read before you write" in prompt


# ---------------------------------------------------------------------------
# Tool adapters
# ---------------------------------------------------------------------------


def _call_tool(tool_obj, **kwargs):
    """Invoke a strands-decorated tool's underlying function."""
    target = getattr(tool_obj, "_tool_func", None) or getattr(
        tool_obj, "original_function", None
    )
    if target is None and callable(tool_obj):
        target = tool_obj
    return target(**kwargs)


def test_run_tests_tool_returns_structured_json(no_test_execution, workspace_with_tests):
    payload = json.loads(
        _call_tool(engineer.run_tests, workspace=str(workspace_with_tests))
    )
    assert payload["outcome"] == "not_executed"
    assert payload["passed"] is False


def test_push_commit_tool_rejects_non_json_input():
    payload = json.loads(
        _call_tool(
            engineer.push_commit,
            repo_full_name="owner/repo",
            branch="b",
            message="m",
            files_json="not json",
        )
    )
    assert "error" in payload


def test_push_commit_tool_rejects_a_non_array_payload():
    payload = json.loads(
        _call_tool(
            engineer.push_commit,
            repo_full_name="owner/repo",
            branch="b",
            message="m",
            files_json='{"path": "a.py"}',
        )
    )
    assert "must be a JSON array" in payload["error"]


def test_create_branch_tool_returns_an_error_string_not_an_exception():
    payload = json.loads(
        _call_tool(engineer.create_branch, repo_full_name="owner/repo", issue_number=-1)
    )
    assert "error" in payload


# ---------------------------------------------------------------------------
# Boundaries (design.md section 3)
# ---------------------------------------------------------------------------


def _imported_modules(module):
    """Every module name this module imports, per the parsed AST."""
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
    """Every attribute/function name that appears as a call target."""
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


def test_engineer_does_not_write_dynamodb():
    # The Orchestrator is the only component that persists state (design.md
    # section 3). Checked against the AST so the module can discuss the boundary
    # in its docstring without tripping its own test.
    assert not any("dynamo_tools" in name for name in _imported_modules(engineer))
    called = _called_names(engineer)
    assert "write_state" not in called
    assert "transition" not in called


def test_engineer_does_not_open_prs_or_post_comments():
    # The Communicator is the only agent that does either.
    called = _called_names(engineer)
    for forbidden in (
        "create_pull",
        "create_pull_request",
        "create_issue_comment",
        "create_comment",
        "create_review",
    ):
        assert forbidden not in called


def test_engineer_never_passes_a_shell_argument():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(engineer))
    assert [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell"
    ] == []
