"""webapp/key_store.py — LLM API key management from the Ops page.

The key lives in .env (gitignored — the ONLY place secrets go). The LLM
client re-reads .env on every construction ({**os.environ, **load_env()},
file wins), so a save from the UI takes effect on the next scoring run
with no restart. The key is NEVER rendered back to the page — only a
masked tail confirms it's set.
"""

from __future__ import annotations

from pathlib import Path

from jobscout.core import config as core_config
from jobscout.core import paths as core_paths
from jobscout.webapp.stores import config_store as cs


class KeyStoreError(Exception):
    pass


def _env_path() -> Path:
    return core_paths.env_path()


def _current_env() -> dict[str, str]:
    """Same precedence the LLM client uses: .env overrides the process."""
    import os

    return {**os.environ, **core_config.load_env()}


def status() -> dict:
    """Masked view for the UI: is the key set, its tail, the base URL."""
    settings = core_config.load_settings()
    key = (_current_env().get(settings.llm.api_key_env) or "").strip()
    base = (_current_env().get(settings.llm.base_url_env) or "").strip()
    return {
        "key_env": settings.llm.api_key_env,
        "base_url_env": settings.llm.base_url_env,
        "key_set": bool(key),
        "key_tail": key[-4:] if key else "",
        "base_url": base,
    }


def _upsert(path: Path, env_name: str, value: str) -> None:
    cs.env_upsert(path, env_name, value)


def _drop(path: Path, env_name: str) -> None:
    cs.env_drop(path, env_name)


def save_key(raw_key: str) -> dict:
    """Write the API key into .env (replacing any existing line, keeping
    everything else). Returns the new masked status."""
    key_env = core_config.load_settings().llm.api_key_env
    key = (raw_key or "").strip().strip("'\"")
    if not key:
        raise KeyStoreError("empty key — nothing to save")
    if any(ch.isspace() for ch in key):
        raise KeyStoreError("the key contains whitespace — paste it verbatim")
    _upsert(_env_path(), key_env, key)
    return status()


# ── CSE (search) keys — the agent's web-search tier ──────────────────────

CSE_KEY_ENV = "JOBSCOUT_CSE_API_KEY"   # canonical name (tools + .env.example)
BRAVE_KEY_ENV = "JOBSCOUT_BRAVE_API_KEY"
CSE_CX_ENV = "JOBSCOUT_CSE_CX"


def cse_status() -> dict:
    env = _current_env()
    key = (env.get(CSE_KEY_ENV) or "").strip()
    cx = (env.get(CSE_CX_ENV) or "").strip()
    return {"key_set": bool(key), "key_tail": key[-4:] if key else "",
            "cx": cx}


def save_cse(*, key: str, cx: str) -> dict:
    """Empty fields leave the existing value untouched; non-empty replace.
    Both live in .env like the LLM key."""
    path = _env_path()
    key = (key or "").strip()
    cx = (cx or "").strip()
    if key:
        if any(ch.isspace() for ch in key):
            raise KeyStoreError("the CSE key contains whitespace")
        _upsert(path, CSE_KEY_ENV, key)
    if cx:
        _upsert(path, CSE_CX_ENV, cx)
    return cse_status()


def clear_cse() -> dict:
    path = _env_path()
    _drop(path, CSE_KEY_ENV)
    _drop(path, CSE_CX_ENV)
    return cse_status()


def brave_status() -> dict:
    key = (_current_env().get(BRAVE_KEY_ENV) or "").strip()
    return {"key_set": bool(key), "key_tail": key[-4:] if key else ""}


def save_brave(raw_key: str) -> dict:
    key = (raw_key or "").strip().strip("'\"")
    if not key:
        raise KeyStoreError("empty key — nothing to save")
    if any(ch.isspace() for ch in key):
        raise KeyStoreError("the key contains whitespace — paste it verbatim")
    _upsert(_env_path(), BRAVE_KEY_ENV, key)
    return brave_status()


def clear_brave() -> dict:
    _drop(_env_path(), BRAVE_KEY_ENV)
    return brave_status()


def clear_key() -> dict:
    """Remove the key line from .env (LLM work degrades gracefully again)."""
    settings = core_config.load_settings()
    _drop(_env_path(), settings.llm.api_key_env)
    return status()
