"""Config loading — YAML → pydantic. Plus a tiny .env reader (no dependency)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from jobscout.core.models import (
    ModelsCfg,
    ProfileCfg,
    Settings,
    Watchlist,
)
from jobscout.core.paths import config_dir, env_path


class ConfigError(Exception):
    """Raised when a config file is missing or invalid."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"expected a mapping at top level of {path}")
    return data


def _load(path: Path, model: type, label: str):
    try:
        return model.model_validate(_load_yaml(path))
    except ConfigError:
        raise
    except Exception as e:  # pydantic ValidationError and friends
        raise ConfigError(f"{label} failed validation ({path}): {e}") from e


def load_settings() -> Settings:
    return _load(config_dir() / "settings.yaml", Settings, "settings.yaml")


def load_models_cfg() -> ModelsCfg:
    return _load(config_dir() / "models.yaml", ModelsCfg, "models.yaml")


def load_profile() -> ProfileCfg:
    return _load(config_dir() / "profile.yaml", ProfileCfg, "profile.yaml")


def load_watchlist() -> Watchlist:
    return _load(config_dir() / "watchlist.yaml", Watchlist, "watchlist.yaml")


def load_env() -> dict[str, str]:
    """Parse .env (KEY=VALUE lines) if present; never raises."""
    path = env_path()
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out
