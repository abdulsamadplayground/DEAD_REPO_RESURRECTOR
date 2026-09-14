"""Env-driven model-provider factory — makes the agent model backend configurable.

design.md describes the agents running on Amazon Bedrock (Claude Sonnet). In the
target AWS account, however, Bedrock is quota/forms-gated, so the operational
provider is **Google Gemini** (validated end-to-end). This module is the single
seam that lets the three sub-agents (Analyst, Engineer, Communicator) pick their
model backend from configuration instead of being hard-wired to Bedrock.

The one public entry point is :func:`get_default_model`, which returns either a
``strands`` ``Model`` instance for the configured provider or ``None`` to mean
"use the ``strands`` / Bedrock default" (i.e. ``Agent(model=None)``). Because
``None`` is the no-config answer, wiring this into the agents' internal default
path is behaviour-identical to today whenever no ``RESURRECTOR_MODEL_*`` env is
set — every existing test keeps passing.

Configuration (all optional; sensible per-provider defaults)
------------------------------------------------------------
- ``RESURRECTOR_MODEL_PROVIDER`` — ``bedrock`` (default) | ``gemini`` | ``openai``.
- ``RESURRECTOR_MODEL_ID`` — the model id. Optional; each provider has a default
  (gemini → ``gemini-3.6-flash``, openai → ``gpt-4o-mini``). For ``bedrock`` an
  unset value means "return ``None``" (strands default); a set value is returned
  as the id string so ``Agent(model="<bedrock-id>")`` pins it.
- ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` — Gemini API key (env fallback order),
  else Secrets Manager id in ``RESURRECTOR_GEMINI_SECRET_ID``
  (default ``/resurrector/gemini-key``).
- ``OPENAI_API_KEY`` — OpenAI API key, else ``RESURRECTOR_OPENAI_SECRET_ID``
  (default ``/resurrector/openai-key``).

Lazy imports
------------
Importing this module must not require ``strands`` or ``google-genai`` to be
importable. ``GeminiModel`` / ``OpenAIModel`` are imported **inside** the
provider branch, only when that provider's model is actually constructed. This
keeps the module import-safe and fast for the many code paths (and tests) that
never touch the model layer.

Caching
-------
The constructed model is cached per process (module-level, mirroring the token
cache in :mod:`src.lambdas.scanner_lambda`). :func:`reset` clears it for tests.

Secret loading mirrors ``scanner_lambda.load_github_token``: env first, then a
lazily-created Secrets Manager client; the ``SecretString`` may be a raw string
or a JSON object carrying a ``key`` / ``api_key`` field. The client is injectable
so tests never need AWS.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROVIDER_ENV_VAR = "RESURRECTOR_MODEL_PROVIDER"
MODEL_ID_ENV_VAR = "RESURRECTOR_MODEL_ID"

GEMINI_API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_SECRET_ID_ENV_VAR = "RESURRECTOR_GEMINI_SECRET_ID"
DEFAULT_GEMINI_SECRET_ID = "/resurrector/gemini-key"
DEFAULT_GEMINI_MODEL_ID = "gemini-3.6-flash"

OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
OPENAI_SECRET_ID_ENV_VAR = "RESURRECTOR_OPENAI_SECRET_ID"
DEFAULT_OPENAI_SECRET_ID = "/resurrector/openai-key"
DEFAULT_OPENAI_MODEL_ID = "gpt-4o-mini"

DEFAULT_PROVIDER = "bedrock"
SUPPORTED_PROVIDERS = frozenset({"bedrock", "gemini", "openai"})


class ModelProviderError(RuntimeError):
    """Raised when the model provider is misconfigured (unknown / missing key)."""


# ---------------------------------------------------------------------------
# Lazily-created Secrets Manager client + model cache
#
# Built on first use rather than at import time so that (a) importing this
# module never requires boto3 credentials and (b) tests can inject a fake
# secrets client without any AWS at all.
# ---------------------------------------------------------------------------

_SECRETS_CLIENT: Any = None
_MODEL_CACHE: Any = None
_MODEL_CACHED: bool = False


def _secrets_client() -> Any:
    """Return the process-wide Secrets Manager client, created on first use."""
    global _SECRETS_CLIENT
    if _SECRETS_CLIENT is None:
        import boto3  # local import keeps module import cheap / boto3-optional

        _SECRETS_CLIENT = boto3.client("secretsmanager")
    return _SECRETS_CLIENT


def reset() -> None:
    """Drop the cached model and secrets client (used by tests)."""
    global _SECRETS_CLIENT, _MODEL_CACHE, _MODEL_CACHED
    _SECRETS_CLIENT = None
    _MODEL_CACHE = None
    _MODEL_CACHED = False


def _load_secret(secret_id: str, *, secrets_client: Any = None) -> str:
    """Load a secret value from Secrets Manager, mirroring ``load_github_token``.

    The ``SecretString`` may be a raw string, or a JSON object carrying a
    ``key`` / ``api_key`` field. ``secrets_client`` is injectable so tests never
    need AWS.
    """
    client = secrets_client if secrets_client is not None else _secrets_client()
    response = client.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString")
    if not raw:
        raise ModelProviderError(f"secret {secret_id!r} has no SecretString value")

    key = raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        key = parsed.get("key") or parsed.get("api_key") or ""
    if not key:
        raise ModelProviderError(f"secret {secret_id!r} does not contain a key")
    return key


def _resolve_api_key(
    *,
    env_vars: tuple[str, ...],
    secret_id_env_var: str,
    default_secret_id: str,
    secrets_client: Any = None,
) -> str:
    """Resolve an API key: env vars first (in order), then Secrets Manager.

    Mirrors ``scanner_lambda.load_github_token``'s resolution order — a local
    env var (local testing / SAM local) wins over Secrets Manager.
    """
    for env_var in env_vars:
        value = os.environ.get(env_var)
        if value:
            return value

    secret_id = os.environ.get(secret_id_env_var, default_secret_id)
    return _load_secret(secret_id, secrets_client=secrets_client)


# ---------------------------------------------------------------------------
# Provider builders (strands models imported lazily, inside the branch)
# ---------------------------------------------------------------------------


def _build_gemini_model(*, model_id: str, secrets_client: Any = None) -> Any:
    """Construct a strands ``GeminiModel`` for the configured key / model id."""
    key = _resolve_api_key(
        env_vars=GEMINI_API_KEY_ENV_VARS,
        secret_id_env_var=GEMINI_SECRET_ID_ENV_VAR,
        default_secret_id=DEFAULT_GEMINI_SECRET_ID,
        secrets_client=secrets_client,
    )
    from strands.models.gemini import GeminiModel  # lazy: only when building gemini

    return GeminiModel(client_args={"api_key": key}, model_id=model_id)


def _build_openai_model(*, model_id: str, secrets_client: Any = None) -> Any:
    """Construct a strands ``OpenAIModel`` for the configured key / model id."""
    key = _resolve_api_key(
        env_vars=(OPENAI_API_KEY_ENV_VAR,),
        secret_id_env_var=OPENAI_SECRET_ID_ENV_VAR,
        default_secret_id=DEFAULT_OPENAI_SECRET_ID,
        secrets_client=secrets_client,
    )
    from strands.models.openai import OpenAIModel  # lazy: only when building openai

    return OpenAIModel(client_args={"api_key": key}, model_id=model_id)


def _build_model(*, secrets_client: Any = None) -> Any:
    """Construct (uncached) the model for the configured provider.

    Returns a strands ``Model`` instance for gemini/openai, a Bedrock model-id
    string when ``bedrock`` + ``RESURRECTOR_MODEL_ID`` is set, or ``None`` for
    the plain Bedrock default.
    """
    provider = (os.environ.get(PROVIDER_ENV_VAR) or DEFAULT_PROVIDER).strip().lower()
    model_id = (os.environ.get(MODEL_ID_ENV_VAR) or "").strip()

    if provider not in SUPPORTED_PROVIDERS:
        raise ModelProviderError(
            f"unknown model provider {provider!r}; expected one of "
            f"{', '.join(sorted(SUPPORTED_PROVIDERS))} "
            f"(set {PROVIDER_ENV_VAR})"
        )

    if provider == "bedrock":
        # None -> strands' own Bedrock default (behaviour-identical to today).
        # A pinned id -> return the string so Agent(model="<id>") uses it.
        return model_id or None

    if provider == "gemini":
        return _build_gemini_model(
            model_id=model_id or DEFAULT_GEMINI_MODEL_ID,
            secrets_client=secrets_client,
        )

    # provider == "openai"
    return _build_openai_model(
        model_id=model_id or DEFAULT_OPENAI_MODEL_ID,
        secrets_client=secrets_client,
    )


def get_default_model(*, secrets_client: Any = None) -> Optional[Any]:
    """Return the configured model backend, or ``None`` for the Bedrock default.

    Resolves ``RESURRECTOR_MODEL_PROVIDER`` (default ``bedrock``) and returns:

    - ``None`` — use the ``strands`` / Bedrock default (``bedrock`` with no
      ``RESURRECTOR_MODEL_ID``). This is the no-config answer, so the agents'
      default path is unchanged from today.
    - a Bedrock model-id ``str`` — ``bedrock`` with ``RESURRECTOR_MODEL_ID`` set.
    - a strands ``GeminiModel`` / ``OpenAIModel`` instance — for ``gemini`` /
      ``openai``.

    The result is cached for the life of the process (like the token cache in
    :mod:`src.lambdas.scanner_lambda`); call :func:`reset` to clear it in tests.
    ``secrets_client`` is injectable so tests never need AWS.
    """
    global _MODEL_CACHE, _MODEL_CACHED
    if _MODEL_CACHED:
        return _MODEL_CACHE

    model = _build_model(secrets_client=secrets_client)
    _MODEL_CACHE = model
    _MODEL_CACHED = True
    return model
