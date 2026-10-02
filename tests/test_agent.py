"""Tests for the P5 agent harness: tool loop, caps, reports, tool safety."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.agent import tools as agent_tools
from jobscout.agent.harness import FakeAgentModel, run_morning
from jobscout.core import db as core_db
from jobscout.core.schema import Settings, TargetCfg
from jobscout.sources.postings.base import make_client


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
    from jobscout.core import watchlist as wlmod

    w = wlmod.Watchlist()
    w.B.append(
        __import__("jobscout.core.schema", fromlist=["WatchlistEntry"]).WatchlistEntry(
            name="Wintermute", domain="wintermute.com"
        )
    )
    return w


SETTINGS = Settings(discovery=__import__("jobscout.core.schema", fromlist=["DiscoveryCfg"]).DiscoveryCfg())

PROFILE = __import__("jobscout.core.schema", fromlist=["ProfileCfg"]).ProfileCfg(
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


# ── suggested_tier: the agent's recommendation, recorded separately ───────────


def _add(ctx, **kw):
    args = {"name": "Newco", "domain": "newco.com", "note": "hiring rust devs",
            "suggested_tier": "B"}
    args.update(kw)
    return agent_tools.execute("add_company", args, ctx)


def _ctx(conn, wl):
    return agent_tools.AgentCtx(conn=conn, wl=wl, profile=PROFILE,
                               settings=SETTINGS, env={}, client=None)


def test_add_company_records_the_suggested_tier(conn, wl):
    result = _add(_ctx(conn, wl), suggested_tier="A")
    assert "suggested tier A" in result

    row = conn.execute(
        "SELECT tier, suggested_tier FROM companies WHERE id = 'newco'").fetchone()
    # the suggestion is advice only — the company still enters as a candidate
    assert row["tier"] == "candidate"
    assert row["suggested_tier"] == "A"


def test_suggested_tier_does_not_promote(conn, wl):
    _add(_ctx(conn, wl), suggested_tier="A")
    from jobscout.core import watchlist as wlmod

    assert wlmod.find(wl, "Newco")[0] == "candidates", (
        "suggesting A must not move the company into tier A")


@pytest.mark.parametrize("tier", ["A", "B", "C", "candidate"])
def test_every_valid_suggested_tier_is_accepted(conn, wl, tier):
    assert "rejected" not in _add(_ctx(conn, wl), suggested_tier=tier)
    row = conn.execute(
        "SELECT suggested_tier FROM companies WHERE id = 'newco'").fetchone()
    assert row["suggested_tier"] == tier


def test_invalid_suggested_tier_is_refused(conn, wl):
    result = _add(_ctx(conn, wl), suggested_tier="S")
    assert "rejected" in result
    assert "suggested_tier must be one of" in result
    # nothing was written
    assert conn.execute(
        "SELECT COUNT(*) n FROM companies WHERE id = 'newco'").fetchone()["n"] == 0


def test_missing_suggested_tier_is_not_guessed(conn, wl):
    """No advice in, no advice stored — but the agent is told it was missing."""
    result = _add(_ctx(conn, wl), suggested_tier=None)
    assert "added" in result
    assert "NO suggested tier given" in result
    row = conn.execute(
        "SELECT suggested_tier FROM companies WHERE id = 'newco'").fetchone()
    assert row["suggested_tier"] is None


def test_tool_spec_requires_and_documents_the_tier():
    spec = next(t for t in agent_tools.TOOLS_SPEC
                if t["function"]["name"] == "add_company")
    params = spec["function"]["parameters"]
    assert "suggested_tier" in params["required"]
    prop = params["properties"]["suggested_tier"]
    assert prop["enum"] == ["A", "B", "C", "candidate"]
    assert "A =" in prop["description"] and "candidate" in prop["description"]
    assert "suggested_tier" in spec["function"]["description"]


def test_prompts_all_teach_the_tier_rubric():
    from jobscout.agent.brief import build_brief

    for text in (agent_tools.SYSTEM_PROMPT,
                 build_brief(None, PROFILE, SETTINGS)):
        for tier in ("A", "B", "C", "candidate"):
            assert tier in text, f"{tier} missing from the prompt"
        assert "suggested_tier" in text
        assert "profile" in text.lower()
    # the brief must put the profile in front of the agent
    assert "market-data" in build_brief(None, PROFILE, SETTINGS)


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
    from datetime import UTC, datetime

    report = tmp_path / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.md"
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
    monkeypatch.setattr("jobscout.core.paths.config_dir",
                        lambda: tmp_path)
    core_config.set_discovery_mode("agent")
    assert "mode: \"agent\"" in settings_file.read_text()
    core_config.set_discovery_mode("off")
    assert 'mode: "off"' in settings_file.read_text()
    with pytest.raises(core_config.ConfigError):
        core_config.set_discovery_mode("bogus")


# ── brief targeting (hunting focus for the morning agent) ───────────────


def test_brief_carries_targeting():
    import sqlite3

    from jobscout.agent.brief import build_brief
    from jobscout.core.config import load_settings
    from jobscout.core.schema import ProfileCfg, TargetCfg

    profile = ProfileCfg(target=TargetCfg(
        roles=["backend engineer"], seniorities=["junior"],
        locations=["Singapore", "Amsterdam"], primary_locations=["Singapore"],
        stack=["rust"], domains=["execution"],
    ))
    brief = build_brief(sqlite3.connect(":memory:"), profile, load_settings())
    assert "junior" in brief                      # level derived, not hardcoded
    assert "senior engineer" not in brief
    assert "Singapore (preferred)" in brief       # priority flows into the brief
    assert "Amsterdam" in brief
    assert "backend engineer" in brief


def test_brief_targeting_defaults():
    import sqlite3

    from jobscout.agent.brief import build_brief
    from jobscout.core.config import load_settings
    from jobscout.core.schema import ProfileCfg

    brief = build_brief(sqlite3.connect(":memory:"), ProfileCfg(),
                        load_settings())
    assert "any level" in brief
    assert "locations=any" in brief
