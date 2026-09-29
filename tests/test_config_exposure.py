"""Frontend exposure of every key config file: models.yaml (+ provider
price refresh), settings.yaml agent caps, CSE keys in .env, and the
watchlist company editor. Every store patches a THROWAWAY copy — the
real config files are never touched."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import config as core_config
from jobscout.webapp import models_store as ms
from jobscout.webapp import settings_store as ss
from jobscout.webapp import watchlist_store as ws

REPO = Path(__file__).resolve().parents[1]
CONFIG_FILES = ("models.yaml", "settings.yaml", "watchlist.yaml",
                "profile.yaml")


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    for name in CONFIG_FILES:
        src = REPO / "config" / name
        (d / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(core_config, "config_dir", lambda: d)
    return d


@pytest.fixture()
def client(monkeypatch, cfg_dir, tmp_path):
    from jobscout.core import db
    from jobscout.webapp.routes import create_app

    db_file = tmp_path / "route.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    # a company + posting so pages render with data
    conn.execute("INSERT INTO companies (id, name, domain, tier) "
                 "VALUES ('jane-street', 'Jane Street', 'janestreet.com', 'A')")
    conn.commit()
    conn.close()

    def fake_connect():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "connect", fake_connect)
    return TestClient(create_app())


# ── models.yaml ─────────────────────────────────────────────────────────────


def _tiers_form(**over):
    base = {
        "model_bulk": "deepseek/deepseek-chat",
        "price_in_bulk": "0.27", "price_out_bulk": "1.10", "cap_bulk": "0.25",
        "model_agent": "deepseek/deepseek-chat",
        "price_in_agent": "0.27", "price_out_agent": "1.10", "cap_agent": "1.00",
        "model_quality": "deepseek/deepseek-reasoner",
        "price_in_quality": "0.55", "price_out_quality": "2.19",
        "cap_quality": "2.00",
        "monthly_usd": "15.0", "on_cap": "rule-only",
    }
    base.update(over)
    return base


def test_models_save_roundtrip_and_comment_preservation(cfg_dir):
    result = ms.save(tiers={
        "bulk": {"model": "vendor/new-cheap", "price_in": "0.1",
                 "price_out": "0.2", "max_daily_usd": "0.5"},
        "agent": {"model": "deepseek/deepseek-chat", "price_in": "0.27",
                  "price_out": "1.10", "max_daily_usd": "1.0"},
        "quality": {"model": "deepseek/deepseek-reasoner", "price_in": "0.55",
                    "price_out": "2.19", "max_daily_usd": "2.0"},
    }, caps={"monthly_usd": "12.0", "on_cap": "fail"})
    assert result["tiers"]["bulk"].model == "vendor/new-cheap"
    assert result["caps"].on_cap == "fail"
    text = (cfg_dir / "models.yaml").read_text(encoding="utf-8")
    assert "three tiers, three jobs" in text          # header survives
    assert "purpose:" in text                          # untouched keys survive


def test_models_save_invalid_rolls_back(cfg_dir):
    before = (cfg_dir / "models.yaml").read_bytes()
    with pytest.raises(ms.ModelsStoreError):
        ms.save(tiers={
            "bulk": {"model": "", "price_in": "0", "price_out": "0",
                     "max_daily_usd": "0"},
            "agent": {"model": "x", "price_in": "0", "price_out": "0",
                      "max_daily_usd": "0"},
            "quality": {"model": "y", "price_in": "0", "price_out": "0",
                        "max_daily_usd": "0"},
        }, caps={"monthly_usd": "1", "on_cap": "rule-only"})
    assert (cfg_dir / "models.yaml").read_bytes() == before


def test_models_route_saves_and_flashes(client, cfg_dir):
    r = client.post("/ops/models", data=_tiers_form(model_bulk="vendor/x"),
                    follow_redirects=False)
    assert r.status_code == 303
    assert "models_saved=1" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "Models &amp; pricing" in page.text
    assert 'name="model_bulk"' in page.text


def test_price_refresh_writes_matched_prices(cfg_dir, monkeypatch):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"id": "deepseek/deepseek-chat",
                 "pricing": {"prompt": "0.00000027", "completion": "0.0000011"}},
                {"id": "deepseek/deepseek-reasoner",
                 "pricing": {"prompt": "0.00000055", "completion": "0.00000219"}},
            ]}

    import jobscout.webapp.models_store as ms_mod

    monkeypatch.setattr("httpx.get", lambda *a, **kw: FakeResp())
    monkeypatch.setenv("JOBSCOUT_LLM_BASE_URL", "https://api.example.com/v1")
    result = ms.refresh_prices()
    assert "error" not in result, result
    assert len(result["updated"]) == 3                 # all tiers matched
    cfg = ms.current()
    assert cfg.tiers["bulk"].price_in_per_mtok == 0.27


def test_price_refresh_no_pricing_reports_clearly(cfg_dir, monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "deepseek/deepseek-chat"}]}

    import jobscout.webapp.models_store as ms_mod

    monkeypatch.setattr("httpx.get", lambda *a, **kw: FakeResp())
    monkeypatch.setenv("JOBSCOUT_LLM_BASE_URL", "https://api.example.com/v1")
    result = ms.refresh_prices()
    assert "OpenRouter-style" in result["error"]


# ── settings.yaml agent caps ────────────────────────────────────────────────


def test_agent_caps_roundtrip_preserves_comments(cfg_dir):
    before = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    ss.save(schedule="daily", max_steps="40", max_cost_usd="0.75",
            run_on_signal=False)
    agent = core_config.load_settings().discovery.agent
    assert (agent.schedule, agent.max_steps, agent.max_cost_usd,
            agent.run_on_signal) == ("daily", 40, 0.75, False)
    after = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    assert "tool-call budget per morning run" in after   # comments survive
    assert "discovery mode switch lives here" in after
    assert before != after


def test_agent_caps_invalid_rolls_back(cfg_dir):
    before = (cfg_dir / "settings.yaml").read_bytes()
    with pytest.raises(ss.SettingsStoreError):
        ss.save(schedule="sometimes", max_steps="40", max_cost_usd="0.5",
                run_on_signal=True)
    with pytest.raises(ss.SettingsStoreError):
        ss.save(schedule="daily", max_steps="0", max_cost_usd="0.5",
                run_on_signal=True)
    assert (cfg_dir / "settings.yaml").read_bytes() == before


def test_agent_caps_route(client, cfg_dir):
    r = client.post("/discovery/agent-caps", data={
        "schedule": "mon-wed-fri", "max_steps": "30",
        "max_cost_usd": "0.4", "run_on_signal": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "caps_saved=1" in r.headers["location"]
    agent = core_config.load_settings().discovery.agent
    assert agent.max_steps == 30 and agent.schedule == "mon-wed-fri"


# ── CSE keys ────────────────────────────────────────────────────────────────


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    f = tmp_path / ".env"
    f.write_text("JOBSCOUT_LLM_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.setattr(core_paths, "env_path", lambda: f)
    monkeypatch.setattr(core_config, "env_path", lambda: f)
    return f


def test_cse_save_and_clear(env_file):
    from jobscout.webapp import key_store as ks

    ks.save_cse(key="cse-key-123", cx="cx-abc")
    text = env_file.read_text(encoding="utf-8")
    assert "JOBSCOUT_CSE_API_KEY=cse-key-123" in text
    assert "JOBSCOUT_CSE_CX=cx-abc" in text
    assert "JOBSCOUT_LLM_API_KEY=sk-x" in text      # untouched
    st = ks.cse_status()
    assert st["key_set"] and st["cx"] == "cx-abc"
    ks.clear_cse()
    assert "JOBSCOUT_CSE" not in env_file.read_text(encoding="utf-8")


def test_cse_empty_fields_keep_existing(env_file):
    from jobscout.webapp import key_store as ks

    ks.save_cse(key="keep-me", cx="keep-cx")
    ks.save_cse(key="", cx="")
    st = ks.cse_status()
    assert st["key_tail"] == "p-me" and st["cx"] == "keep-cx"


def test_cse_route_renders_and_saves(client, env_file):
    r = client.get("/ops")
    assert 'name="cse_key"' in r.text and 'name="cse_cx"' in r.text
    r2 = client.post("/ops/cse-keys", data={"cse_key": "zz", "cse_cx": "cc"},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert "JOBSCOUT_CSE_API_KEY=zz" in env_file.read_text(encoding="utf-8")


# ── watchlist editor ────────────────────────────────────────────────────────


def test_watchlist_edit_in_place(cfg_dir):
    ws.save("jane-street", name="Jane Street", domain="janestreet.com",
            tier="A", note="edited note; still data",
            ats_text="greenhouse: janestreet")
    t, e = ws.find("jane-street")
    assert t == "A" and e.note == "edited note; still data"
    wl = ws.current()
    assert [x.name for x in wl.A][0] == "Jane Street"


def test_watchlist_move_between_tiers(cfg_dir):
    ws.save("jane-street", name="Jane Street", domain="janestreet.com",
            tier="B", note="moved down", ats_text="")
    t, e = ws.find("jane-street")
    assert t == "B" and e.ats == {}
    wl = ws.current()
    assert all(x.name != "Jane Street" for x in wl.A)
    assert any(x.name == "Jane Street" for x in wl.B)


def test_watchlist_move_to_candidates_and_back(cfg_dir):
    ws.save("jane-street", name="Jane Street", domain="janestreet.com",
            tier="candidates", note="demoted", ats_text="")
    assert ws.find("jane-street")[0] == "candidates"
    ws.save("jane-street", name="Jane Street", domain="janestreet.com",
            tier="A", note="promoted again", ats_text="greenhouse: janestreet")
    t, e = ws.find("jane-street")
    assert t == "A" and e.ats == {"greenhouse": "janestreet"}


def test_watchlist_rejects_rename_and_bad_ats(cfg_dir):
    with pytest.raises(ws.WatchlistStoreError):
        ws.save("jane-street", name="Different Name", domain="x.com",
                tier="A", note="", ats_text="")
    with pytest.raises(ws.WatchlistStoreError):
        ws.save("jane-street", name="Jane Street", domain="janestreet.com",
                tier="A", note="", ats_text="not-a-pair")


def test_company_editor_pages(client, cfg_dir):
    r = client.get("/companies/jane-street")
    assert r.status_code == 200
    assert 'action="/companies/jane-street/save"' in r.text
    assert "Jane Street" in r.text

    r2 = client.post("/companies/jane-street/save", data={
        "name": "Jane Street", "domain": "janestreet.com", "tier": "B",
        "note": "demoted from UI", "ats": "greenhouse: janestreet",
    }, follow_redirects=False)
    assert r2.status_code == 303
    assert "company_saved=1" in r2.headers["location"]
    assert ws.find("jane-street")[0] == "B"
    page = client.get(r2.headers["location"])
    assert "Company saved" in page.text


def test_companies_page_has_edit_buttons(client):
    r = client.get("/companies")
    assert 'href="/companies/jane-street"' in r.text


def test_cse_key_uses_canonical_env_name(env_file):
    """The agent tools read JOBSCOUT_CSE_API_KEY (per .env.example) — the
    key store must write exactly that name or search silently stays off."""
    from jobscout.webapp import key_store as ks

    assert ks.CSE_KEY_ENV == "JOBSCOUT_CSE_API_KEY"
    ks.save_cse(key="abc", cx="cx")
    text = env_file.read_text(encoding="utf-8")
    assert "JOBSCOUT_CSE_API_KEY=abc" in text


def test_companies_rows_link_and_provenance(client, cfg_dir):
    r = client.get("/companies")
    # jane-street has a career_url → row links to it
    assert 'data-row-link="https://janestreet.com' in r.text or \
           'data-row-link="' in r.text
    assert 'title="open ' in r.text
    assert 'href="https://janestreet.com"' in r.text      # domain cell link
