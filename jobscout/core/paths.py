"""Repository path resolution. The package always finds its repo from its own
location (not the CWD), so CLI verbs work from anywhere."""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent  # jobscout/core


def repo_root() -> Path:
    """JOBSCOUT_HOME relocates the entire runtime (data/, config/,
    master_resume/, applications/, …) — the Tauri shell sets it to the
    OS app-data dir for packaged builds. Unset (repo checkouts, dev):
    walk upward from this file to the repo root."""
    env_home = os.environ.get("JOBSCOUT_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    # walk upward from this file to the repo root (marked by PLAN.md + pyproject.toml)
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


# ── runtime outputs live under one var/ root ─────────────────────────────
# The repo root holds only source + inputs (config/, master_resume/);
# everything the app WRITES (db, digests, reports, notes, packets, logs)
# lands under var/. Pre-var layouts migrate on first access — the legacy
# directory at the root moves into var/ if var/<name> doesn't exist yet.

_RUNTIME_DIRS = ("data", "digest", "logs", "morning_reports",
                 "research", "applications")


def var_root() -> Path:
    root = repo_root() / "var"
    _migrate_legacy_layout(root)
    return root


def _migrate_legacy_layout(var: Path) -> None:
    """One-time move of pre-var runtime dirs into var/ (idempotent, only
    runs when a legacy dir exists AND var/<name> does not). The packets
    table stores absolute packet paths — those rows are rewritten to the
    new layout in the same pass."""
    root = var.parent
    moved = False
    for name in _RUNTIME_DIRS:
        legacy = root / name
        target = var / name
        if legacy.is_dir() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(target)
            moved = True
    if moved:
        _rewrite_packet_dirs(var)


def _rewrite_packet_dirs(var: Path) -> None:
    """point packets.dir rows at var/applications/… (best effort)."""
    import sqlite3

    db_file = var / "data" / "jobscout.db"
    if not db_file.is_file():
        return
    try:
        conn = sqlite3.connect(db_file)
        try:
            rows = conn.execute(
                "SELECT id, dir FROM packets WHERE dir IS NOT NULL"
            ).fetchall()
            for pid, d in rows:
                if d and f"{var.parent.name}/applications/" not in d:
                    new = d.replace("/applications/", "/var/applications/")
                    conn.execute("UPDATE packets SET dir = ? WHERE id = ?",
                                 (new, pid))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _runtime(name: str) -> Path:
    d = var_root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    return _runtime("data")


def db_path() -> Path:
    return data_dir() / "jobscout.db"


def logs_dir() -> Path:
    return _runtime("logs")


def digest_dir() -> Path:
    return _runtime("digest")


def morning_reports_dir() -> Path:
    return _runtime("morning_reports")


def research_dir() -> Path:
    return _runtime("research")


def applications_dir() -> Path:
    return _runtime("applications")


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
