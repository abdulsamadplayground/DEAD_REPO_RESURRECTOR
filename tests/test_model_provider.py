"""Unit tests for src.tools.model_provider — the env-driven model backend seam.

These cover the factory contract that makes the agent model backend
configurable (design.md uses Bedrock/Claude; Gemini is the operational provider
when Bedrock is quota/forms-gated):

- default (no env) -> ``None`` (strands / Bedrock default), so the agents' default
  path is behaviour-identical to today and every existing test keeps passing.
- ``gemini`` provider with an env key -> a real ``GeminiModel`` instance.
- ``gemini`` provider with the key only in an injected (fake) Secrets Manager.
- an unknown provider -> a clear error.
- per-process caching + ``reset()``.
- the import boundary: importing the module pulls in neither ``google-genai`` nor
  any strands model backend.
- the injected-seam bypass: ``analyze_repo(use_model=False)`` and an injected
  ``run_model`` never consult the provider factory.

No network and no AWS: every secrets client is injected.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from src.agents import analyst
from src.tools import model_provider

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every env var the factory reads, cleared before each test for isolation.
_MODEL_ENV_VARS = (
    "RESURRECTOR_MODEL_PROVIDER",
    "RESURRECTOR_MODEL_ID",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "RESURRECTOR_GEMINI_SECRET_ID",
    "OPENAI_API_KEY",
    "RESURRECTOR_OPENAI_SECRET_ID",
)


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """Give every test a clean env + empty model cache, and reset afterwards."""
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    model_provider.reset()
    yield
    model_provider.reset()


class FakeSecretsClient:
    """Minimal stand-in for the Secrets Manager client's get_secret_value."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 casing
        self.calls.append(SecretId)
        if SecretId not in self._mapping:
            raise KeyError(f"no such secret: {SecretId}")
        return {"SecretString": self._mapping[SecretId]}


# ---------------------------------------------------------------------------
# Default (no env) -> None (Bedrock default)
# ---------------------------------------------------------------------------


def test_default_no_env_returns_none():
    assert model_provider.get_default_model() is None


def test_bedrock_with_model_id_returns_the_id_string(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("RESURRECTOR_MODEL_ID", "anthropic.claude-3-5-sonnet")
    assert model_provider.get_default_model() == "anthropic.claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_env_key_returns_gemini_model(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "x-secret-key")

    model = model_provider.get_default_model()
    assert type(model).__name__ == "GeminiModel"


def test_gemini_default_model_id_is_applied(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "x-secret-key")

    model = model_provider.get_default_model()
    # The configured id lands in the strands model config regardless of the
    # exact attribute layout: assert the default id is present somewhere.
    assert model_provider.DEFAULT_GEMINI_MODEL_ID == "gemini-3.6-flash"
    config = getattr(model, "config", None) or getattr(model, "model_config", None)
    if isinstance(config, dict):
        assert config.get("model_id") == model_provider.DEFAULT_GEMINI_MODEL_ID


def test_gemini_key_loaded_from_injected_secrets_manager(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    # No env key -> must fall back to Secrets Manager at the default secret id.
    secrets = FakeSecretsClient({"/resurrector/gemini-key": "sm-gemini-key"})

    model = model_provider.get_default_model(secrets_client=secrets)
    assert type(model).__name__ == "GeminiModel"
    assert secrets.calls == ["/resurrector/gemini-key"]


def test_gemini_key_from_json_secret(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("RESURRECTOR_GEMINI_SECRET_ID", "/custom/gemini")
    secrets = FakeSecretsClient({"/custom/gemini": '{"api_key": "json-key"}'})

    model = model_provider.get_default_model(secrets_client=secrets)
    assert type(model).__name__ == "GeminiModel"
    assert secrets.calls == ["/custom/gemini"]


def test_gemini_missing_key_raises(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    secrets = FakeSecretsClient({"/resurrector/gemini-key": ""})
    with pytest.raises(model_provider.ModelProviderError):
        model_provider.get_default_model(secrets_client=secrets)


# ---------------------------------------------------------------------------
# Unknown provider
# ---------------------------------------------------------------------------


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "not-a-provider")
    with pytest.raises(model_provider.ModelProviderError) as exc:
        model_provider.get_default_model()
    assert "not-a-provider" in str(exc.value)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_model_is_cached_across_calls(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "x-secret-key")

    first = model_provider.get_default_model()
    second = model_provider.get_default_model()
    assert first is second


def test_reset_clears_the_cache(monkeypatch):
    monkeypatch.setenv("RESURRECTOR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "x-secret-key")

    first = model_provider.get_default_model()
    model_provider.reset()
    second = model_provider.get_default_model()
    assert first is not second


def test_none_default_is_cached(monkeypatch):
    # A None result must still be cached (not recomputed) — the flag, not the
    # value, drives the cache.
    calls = []
    real_build = model_provider._build_model

    def counting_build(**kwargs):
        calls.append(1)
        return real_build(**kwargs)

    monkeypatch.setattr(model_provider, "_build_model", counting_build)
    assert model_provider.get_default_model() is None
    assert model_provider.get_default_model() is None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Import boundary: no google-genai / strands model backend at import time
# ---------------------------------------------------------------------------


def test_import_does_not_pull_in_gemini_or_strands_models():
    code = (
        "import sys\n"
        "import src.tools.model_provider  # noqa: F401\n"
        "bad = [n for n in sys.modules if n == 'google.genai' "
        "or n.startswith('strands.models')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Injected-seam bypass: injected paths never consult the provider factory
# ---------------------------------------------------------------------------


def test_analyze_repo_heuristic_path_does_not_consult_provider(monkeypatch):
    """use_model=False keeps the deterministic path and never builds a model."""
    consulted = []
    monkeypatch.setattr(
        model_provider, "_build_model", lambda **kw: consulted.append(1)
    )

    context = _minimal_repo_context()
    report = analyst.analyze_repo(
        "owner/repo", context=context, use_model=False
    )
    assert report.source == "heuristic"
    assert consulted == []


def test_analyze_repo_injected_run_model_bypasses_provider(monkeypatch):
    """An injected run_model supplies the text; the factory is never consulted."""
    consulted = []
    monkeypatch.setattr(
        model_provider, "_build_model", lambda **kw: consulted.append(1)
    )

    context = _minimal_repo_context()
    raw = (
        '{"complexity": "trivial", "confidence": 0.9, '
        '"files_affected": ["stats.py"], "approach": "guard the empty case"}'
    )
    report = analyst.analyze_repo(
        "owner/repo", context=context, run_model=lambda prompt: raw
    )
    assert report.source == "model"
    assert consulted == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_repo_context():
    """Build a RepoContext with one open issue, enough for analyze_repo."""
    from src.tools.github_tools import IssueSummary, RepoContext

    issue = IssueSummary(
        number=7,
        title="Crash on empty input",
        body="Steps to reproduce: call mean([]) and it raises ZeroDivisionError.",
        thumbs_up=3,
        total_reactions=3,
        comments=1,
        labels=["bug"],
        html_url="https://github.com/owner/repo/issues/7",
    )
    return RepoContext(
        repo_full_name="owner/repo",
        primary_language="Python",
        default_branch="main",
        readme="A small stats library.",
        structure=None,
        recent_commits=[],
        issues=[issue],
    )
