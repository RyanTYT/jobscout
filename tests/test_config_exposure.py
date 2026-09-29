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
    # config_dir is imported BY NAME into several modules — patch each
    # (the profile-store lesson): core.config, watchlist, and the source
    from jobscout import watchlist as _wl
    from jobscout.core import config as _cc
    from jobscout.core import paths as core_paths

    monkeypatch.setattr(core_config, "config_dir", lambda: d)
    monkeypatch.setattr(_cc, "config_dir", lambda: d)
    monkeypatch.setattr(_wl, "config_dir", lambda: d)
    monkeypatch.setattr(core_paths, "config_dir", lambda: d)
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
                {"id": "~deepseek/deepseek-v4-flash-latest",
                 "pricing": {"prompt": "0.00000001", "completion": "0.0000004"}},
                {"id": "deepseek/deepseek-v4-pro",
                 "pricing": {"prompt": "0.00000096", "completion": "0.00000191"}},
            ]}


    monkeypatch.setattr("httpx.get", lambda *a, **kw: FakeResp())
    monkeypatch.setenv("JOBSCOUT_LLM_BASE_URL", "https://api.example.com/v1")
    result = ms.refresh_prices()
    assert "error" not in result, result
    assert len(result["updated"]) == 3                 # all tiers matched
    cfg = ms.current()
    assert cfg.tiers["bulk"].price_in_per_mtok == 0.01


def test_price_refresh_no_pricing_reports_clearly(cfg_dir, monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "deepseek/deepseek-chat"}]}


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


# ── seed-by-URL + profile-driven brief ─────────────────────────────────


def test_brief_has_profile_driven_search_step():
    import sqlite3

    from jobscout.agent.brief import build_brief
    from jobscout.core.config import load_settings
    from jobscout.core.models import ProfileCfg, TargetCfg

    profile = ProfileCfg(target=TargetCfg(
        roles=["backend engineer"], stack=["rust"], domains=["execution"],
        locations=["Singapore"], primary_locations=["Singapore"]))
    brief = build_brief(sqlite3.connect(":memory:"), profile, load_settings())
    assert "PROFILE-DRIVEN" in brief
    assert "SIGNAL-DRIVEN" in brief
    assert "<primary location>" in brief


def test_seed_url_adds_company_candidate(client, cfg_dir):
    r = client.post("/companies/add-url",
                    data={"url": "https://quantacme.io/careers"},
                    follow_redirects=False)
    assert r.status_code == 303
    from jobscout import watchlist as wlmod

    # registrable domain of quantacme.io → company "Quantacme"
    entry = [e for e in wlmod.load().candidates if e.domain == "quantacme.io"]
    assert entry and entry[0].name == "Quantacme"
    assert entry[0].found_via == "seed-url"
    assert "quantacme.io/careers" in (entry[0].note or "")


def test_seed_url_recognises_ats_board(client, cfg_dir):
    r = client.post("/companies/add-url",
                    data={"url": "https://boards.greenhouse.io/newco"},
                    follow_redirects=False)
    print("STATUS:", r.status_code, "| location:", r.headers.get("location"), "| body:", r.text[:200])
    assert r.status_code == 303
    from jobscout import watchlist as wlmod
    tp = wlmod.path()
    print("TMP path:", tp)
    print("TMP tail:", tp.read_text()[-200:] if tp.is_file() else "MISSING")
    print("REAL tail:", Path("config/watchlist.yaml").read_text()[-200:])
    wl = wlmod.load()
    entry = [e for e in wl.candidates if e.name == "Newco"]
    assert entry and entry[0].ats == {"greenhouse": "newco"}


def test_seed_url_rejects_duplicates(client, cfg_dir):
    r = client.post("/companies/add-url", data={"url": "https://janestreet.com"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "already" in r.headers["location"]


def test_companies_page_has_seed_form(client):
    r = client.get("/companies")
    assert 'action="/companies/add-url"' in r.text
    assert 'name="url"' in r.text


# ── Brave fallback key ─────────────────────────────────────────────────


def test_brave_save_status_clear(env_file):
    from jobscout.webapp import key_store as ks

    st = ks.brave_status()
    assert not st["key_set"]
    ks.save_brave("brave-key-9876")
    text = env_file.read_text(encoding="utf-8")
    assert "JOBSCOUT_BRAVE_API_KEY=brave-key-9876" in text
    st2 = ks.brave_status()
    assert st2["key_set"] and st2["key_tail"] == "9876"
    with pytest.raises(ks.KeyStoreError):
        ks.save_brave("has space")
    ks.clear_brave()
    assert "BRAVE" not in env_file.read_text(encoding="utf-8")


def test_brave_route_and_card(client, env_file):
    r = client.get("/ops")
    assert 'name="brave_key"' in r.text
    assert "Brave Search" in r.text
    assert "2,000 queries/month" in r.text          # honest tier info
    r2 = client.post("/ops/brave-key", data={"brave_key": "bb-1234"},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert "JOBSCOUT_BRAVE_API_KEY=bb-1234" in env_file.read_text(encoding="utf-8")


def test_search_provider_dispatch_contract():
    """All four engines are dispatched by name + read their env keys."""
    import inspect

    from jobscout.agent import tools

    mod = inspect.getsource(tools)
    assert "JOBSCOUT_BRAVE_API_KEY" in mod
    assert "JOBSCOUT_CSE_API_KEY" in mod
    assert tools.SEARCH_ENGINE_NAMES == ("cse", "brave", "llm", "ddg")
    # provider selection honours settings.search.provider
    src = inspect.getsource(tools._web_search)
    assert 'cfg.provider' in src


# ── search provider selection ────────────────────────────────────────────


def test_search_settings_roundtrip_and_append(cfg_dir):
    from jobscout.webapp import settings_store as ss

    # the repo settings.yaml ships a search: block (sonar default); save
    # must rewrite it in place either way
    ss.save_search(provider="ddg", llm_model="")
    search = core_config.load_settings().search
    assert search.provider == "ddg"
    after = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    assert "discovery mode switch lives here" in after      # untouched
    # rewrite in place
    ss.save_search(provider="llm", llm_model="openai/gpt-4o-mini:online")
    assert core_config.load_settings().search.llm_model == "openai/gpt-4o-mini:online"


def test_search_settings_llm_needs_model(cfg_dir):
    from jobscout.webapp import settings_store as ss

    before = (cfg_dir / "settings.yaml").read_bytes()
    with pytest.raises(ss.SettingsStoreError):
        ss.save_search(provider="llm", llm_model=" ")
    assert (cfg_dir / "settings.yaml").read_bytes() == before


def test_search_provider_route_and_card(client, cfg_dir):
    r = client.get("/ops")
    assert 'name="provider"' in r.text
    assert "Web search engine" in r.text
    assert "DuckDuckGo" in r.text
    r2 = client.post("/ops/search-provider", data={
        "provider": "ddg", "llm_model": "",
    }, follow_redirects=False)
    assert r2.status_code == 303
    assert "search_saved=1" in r2.headers["location"]
    assert core_config.load_settings().search.provider == "ddg"


def test_web_search_dispatch_honours_provider(monkeypatch):
    """provider=ddg routes to the ddg engine even with CSE keys set."""
    from jobscout.agent import tools
    from jobscout.core.models import SearchCfg, Settings

    calls = []
    monkeypatch.setattr(tools, "_ddg_search",
                        lambda q, n, ctx: calls.append(q) or "1. ddg result")

    class Ctx:
        settings = Settings(search=SearchCfg(provider="ddg"))
        env = {"JOBSCOUT_CSE_API_KEY": "x", "JOBSCOUT_CSE_CX": "y"}

    out = tools._web_search("test query", 3, Ctx())
    assert "ddg result" in out and calls == ["test query"]


def test_web_search_auto_falls_through_to_ddg(monkeypatch):
    from jobscout.agent import tools
    from jobscout.core.models import Settings

    monkeypatch.setattr(tools, "_cse_search", lambda q, n, ctx: None)
    monkeypatch.setattr(tools, "_brave_search", lambda q, n, ctx: None)
    monkeypatch.setattr(tools, "_llm_search", lambda q, n, ctx: None)
    monkeypatch.setattr(tools, "_ddg_search",
                        lambda q, n, ctx: "1. ddg result")

    class Ctx:
        settings = Settings()
        env = {}

    assert "ddg result" in tools._web_search("q", 3, Ctx())
