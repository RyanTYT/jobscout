"""`jobscout doctor` — health checks for the whole stack (PLAN §10 P0)."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from jobscout.core import db
from jobscout.core.config import (
    ConfigError,
    load_env,
    load_models_cfg,
    load_profile,
    load_settings,
    load_watchlist,
)
from jobscout.core.paths import (
    master_resume_dir,
    repo_root,
)
from jobscout.core.resume import ResumeError, validate_resume


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


def run_checks() -> list[Check]:
    checks: list[Check] = []

    # python
    v = sys.version_info
    if v >= (3, 11):
        checks.append(Check("python", "ok", f"{v.major}.{v.minor}.{v.micro}"))
    else:
        checks.append(Check("python", "fail", f"need ≥3.11, have {v.major}.{v.minor}"))

    # repo root
    try:
        root = repo_root()
        checks.append(Check("repo-root", "ok", str(root)))
    except RuntimeError as e:
        checks.append(Check("repo-root", "fail", str(e)))
        return checks

    # configs
    for label, loader in (
        ("settings.yaml", load_settings),
        ("models.yaml", load_models_cfg),
        ("profile.yaml", load_profile),
        ("watchlist.yaml", load_watchlist),
    ):
        try:
            loader()
            checks.append(Check(f"config:{label}", "ok", "valid"))
        except ConfigError as e:
            checks.append(Check(f"config:{label}", "fail", str(e)))

    # master resume
    try:
        issues = validate_resume()
        errors = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level == "warn"]
        if errors:
            checks.append(Check("master-resume", "fail", "; ".join(i.msg for i in errors)))
        elif warns:
            checks.append(Check("master-resume", "warn", "; ".join(i.msg for i in warns)))
        else:
            checks.append(Check("master-resume", "ok", "schema + referential integrity valid"))
    except ResumeError as e:
        checks.append(Check("master-resume", "fail", str(e)))

    for name in ("narrative.md", "extras.md"):
        p = master_resume_dir() / name
        checks.append(Check(f"master-resume:{name}", "ok" if p.is_file() else "warn",
                            "present" if p.is_file() else "missing (create it)"))

    # database
    status = db.db_status()
    if not status["exists"]:
        checks.append(Check("database", "warn", "not initialised — run `jobscout db init`"))
    elif status["schema_version"] != status["expected_version"]:
        checks.append(Check("database", "fail",
                            f"schema v{status['schema_version']} ≠ expected v{status['expected_version']}"))
    else:
        counts = ", ".join(f"{k}={v}" for k, v in status["counts"].items())
        checks.append(Check("database", "ok", f"v{status['schema_version']} ({counts})"))

    # env / secrets
    env = load_env()
    if env.get("JOBSCOUT_LLM_API_KEY"):
        checks.append(Check("env:llm-key", "ok", "set"))
    else:
        checks.append(Check("env:llm-key", "warn",
                            "JOBSCOUT_LLM_API_KEY not set (needed from P2 — see .env.example)"))
    if env.get("JOBSCOUT_LLM_BASE_URL"):
        checks.append(Check("env:llm-base-url", "ok", env["JOBSCOUT_LLM_BASE_URL"]))
    else:
        checks.append(Check("env:llm-base-url", "warn", "JOBSCOUT_LLM_BASE_URL not set"))
    if env.get("JOBSCOUT_CSE_API_KEY") and env.get("JOBSCOUT_CSE_CX"):
        checks.append(Check("env:cse", "ok", "key + cx set"))
    else:
        checks.append(Check("env:cse", "warn", "Google CSE not set (needed for agent discovery, P5)"))

    # sidecar (P7) — informational only
    sidecar = (root / "../JobPilot").resolve()
    checks.append(Check("sidecar:jobpilot", "ok" if sidecar.is_dir() else "warn",
                        f"{sidecar} {'present' if sidecar.is_dir() else 'not found (P7)'}"))

    return checks
