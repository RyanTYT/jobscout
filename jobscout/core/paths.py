"""Repository path resolution. The package always finds its repo from its own
location (not the CWD), so CLI verbs work from anywhere."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent  # jobscout/core


def repo_root() -> Path:
    """Walk upward from this file to the repo root (marked by PLAN.md + pyproject.toml)."""
    for candidate in _PACKAGE_DIR.parents:
        if (candidate / "PLAN.md").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(
        "jobscout repo root not found — expected PLAN.md + pyproject.toml above "
        f"{_PACKAGE_DIR}. Is the package running from a checkout?"
    )


def config_dir() -> Path:
    return repo_root() / "config"


def master_resume_dir() -> Path:
    return repo_root() / "master_resume"


def data_dir() -> Path:
    return repo_root() / "data"


def db_path() -> Path:
    return data_dir() / "jobscout.db"


def logs_dir() -> Path:
    return repo_root() / "logs"


def digest_dir() -> Path:
    return repo_root() / "digest"


def morning_reports_dir() -> Path:
    return repo_root() / "morning_reports"


def research_dir() -> Path:
    return repo_root() / "research"


def applications_dir() -> Path:
    return repo_root() / "applications"


def env_path() -> Path:
    return repo_root() / ".env"


def ensure_runtime_dirs() -> None:
    for d in (
        data_dir(),
        logs_dir(),
        digest_dir(),
        morning_reports_dir(),
        research_dir(),
        applications_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
