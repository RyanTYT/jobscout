"""Tests for the P5 agent harness: tool loop, caps, reports, tool safety."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.agent import tools as agent_tools
from jobscout.agent.harness import FakeAgentModel, run_morning
from jobscout.core import db as core_db
from jobscout.core.models import Settings, TargetCfg
from jobscout.sources.ats.base import make_client


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    c.execute(
        "INSERT INTO companies (id, name, domain, tier, ats_tokens, non_ats) "
        "VALUES ('wintermute', 'Wintermute', 'wintermute.com', 'B', '{}', 1)"
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def wl():
    from jobscout import watchlist as wlmod

    w = wlmod.Watchlist()
    w.B.append(
        __import__("jobscout.core.models", fromlist=["WatchlistEntry"]).WatchlistEntry(
            name="Wintermute", domain="wintermute.com"
        )
    )
    return w


SETTINGS = Settings(discovery=__import__("jobscout.core.models", fromlist=["DiscoveryCfg"]).DiscoveryCfg())

PROFILE = __import__("jobscout.core.models", fromlist=["ProfileCfg"]).ProfileCfg(
    profile_version="t",
    target=TargetCfg(roles=["quant developer"], stack=["rust"], domains=["market-data"]),
)


def test_db_query_rejects_writes(conn):
    ctx = agent_tools.AgentCtx(conn=conn, wl=None, profile=PROFILE, settings=SETTINGS,
                               env={}, client=None)
    assert "rejected" in agent_tools.execute(
        "db_query", {"sql": "DELETE FROM companies"}, ctx)
    assert "rejected" in agent_tools.execute(
        "db_query", {"sql": "DROP TABLE companies"}, ctx)
    assert "rejected" in agent_tools.execute(
        "db_query", {"sql": "SELECT * FROM sqlite_master"}, ctx)


def test_db_query_forces_limit(conn):
    ctx = agent_tools.AgentCtx(conn=conn, wl=None, profile=PROFILE, settings=SETTINGS,
                               env={}, client=None)
    result = agent_tools.execute(
        "db_query", {"sql": "SELECT name FROM companies"}, ctx)
    assert "1 rows" in result


def test_write_note_sanitizes_paths(conn, tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    monkeypatch.setattr(core_paths, "research_dir", lambda: tmp_path)
    ctx = agent_tools.AgentCtx(conn=conn, wl=None, profile=PROFILE, settings=SETTINGS,
                               env={}, client=None)
    result = agent_tools.execute(
        "write_note",
        {"filename": "../../etc/passwd", "content": "evil"},
        ctx,
    )
    assert result.startswith("note written")
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    # traversal flattened into a safe name — no separators, no parent refs,
    # and the file landed directly in the research dir
    assert "/" not in files[0].name and ".." not in files[0].name
    assert files[0].parent == tmp_path


def test_add_company_rejects_fake_domain(conn, wl):
    ctx = agent_tools.AgentCtx(conn=conn, wl=wl, profile=PROFILE, settings=SETTINGS,
                               env={}, client=None)
    result = agent_tools.execute(
        "add_company", {"name": "Nowhere", "domain": "notadomain", "note": "x"}, ctx)
    assert "rejected" in result


def test_run_morning_full_loop(conn, wl, tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    monkeypatch.setattr(core_paths, "research_dir", lambda: tmp_path)
    monkeypatch.setattr(core_paths, "morning_reports_dir", lambda: tmp_path)
    ctx_client = make_client()
    try:
        result = run_morning(
            FakeAgentModel(), conn, wl, PROFILE, SETTINGS, {}, ctx_client
        )
    finally:
        ctx_client.close()
    assert result["steps"] == 4
    assert result["cost"] == 0.0
    assert "Dry run complete" in result["final"]
    assert result["cap_note"] == ""
    report = tmp_path / "2026-09-28.md"
    assert report.is_file()
    content = report.read_text()
    assert "## Tool log" in content and "get_context" in content
    # the scripted write_note landed in research/
    assert (tmp_path / "agent-dry-run-note.md").is_file()


def test_run_morning_step_cap(conn, wl, tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    monkeypatch.setattr(core_paths, "research_dir", lambda: tmp_path)
    monkeypatch.setattr(core_paths, "morning_reports_dir", lambda: tmp_path)
    ctx_client = make_client()
    try:
        result = run_morning(
            FakeAgentModel(), conn, wl, PROFILE, SETTINGS, {}, ctx_client, max_steps=2
        )
    finally:
        ctx_client.close()
    assert result["steps"] == 2
    assert "STEP CAP" in result["cap_note"]


def test_set_discovery_mode_roundtrip(tmp_path, monkeypatch):
    from jobscout.core import config as core_config

    src = "discovery:\n  mode: hybrid\n  agent:\n    schedule: weekdays\n" 
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(src)
    monkeypatch.setattr(core_config, "config_dir", lambda: tmp_path)
    core_config.set_discovery_mode("agent")
    assert "mode: agent" in settings_file.read_text()
    core_config.set_discovery_mode("off")
    assert "mode: off" in settings_file.read_text()
    with pytest.raises(core_config.ConfigError):
        core_config.set_discovery_mode("bogus")
