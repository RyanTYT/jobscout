"""LLM API key management (Ops page): .env writing with preservation,
masked status, and the POST routes. No real key is ever touched — the
tests point env_path at a throwaway file."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from jobscout.core import config as core_config
from jobscout.core import paths as core_paths
from jobscout.webapp import key_store as ks

ENV_HEADER = "# test env\nOTHER=value\n"


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text(ENV_HEADER, encoding="utf-8")
    monkeypatch.setattr(core_paths, "env_path", lambda: f)
    monkeypatch.setattr(core_config, "env_path", lambda: f)
    return f


def test_save_key_creates_line_and_keeps_rest(env_file):
    ks.save_key("sk-test-1234")
    text = env_file.read_text(encoding="utf-8")
    assert "JOBSCOUT_LLM_API_KEY=sk-test-1234" in text
    assert "# test env" in text and "OTHER=value" in text
    st = ks.status()
    assert st["key_set"] and st["key_tail"] == "1234"


def test_save_key_replaces_existing(env_file):
    env_file.write_text(
        "JOBSCOUT_LLM_API_KEY=old-key-abcd\nOTHER=value\n", encoding="utf-8")
    ks.save_key("sk-new-key-9876")
    text = env_file.read_text(encoding="utf-8")
    assert "old-key" not in text and "JOBSCOUT_LLM_API_KEY=sk-new-key-9876" in text
    assert "OTHER=value" in text


def test_save_key_rejects_empty_and_whitespace(env_file):
    with pytest.raises(ks.KeyStoreError):
        ks.save_key("   ")
    with pytest.raises(ks.KeyStoreError):
        ks.save_key("has space inside")


def test_clear_key(env_file):
    ks.save_key("sk-tmp-0000")
    st = ks.clear_key()
    assert not st["key_set"]
    assert "JOBSCOUT" not in env_file.read_text(encoding="utf-8")


def test_status_reflects_env_file(monkeypatch, env_file):
    st = ks.status()
    assert st["key_set"] is False


# ── routes ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, env_file, tmp_path):
    from jobscout.core import db
    from jobscout.webapp.routes import create_app

    db_file = tmp_path / "route.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    conn.commit()
    conn.close()

    def fake_connect():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "connect", fake_connect)
    return TestClient(create_app())


def test_ops_page_renders_key_card(client):
    r = client.get("/ops")
    assert r.status_code == 200
    assert "LLM credentials" in r.text
    assert 'name="api_key"' in r.text
    assert 'type="password"' in r.text
    assert "never rendered back" in r.text


def test_key_post_saves_and_masks(client, env_file):
    r = client.post("/ops/llm-key", data={"api_key": "sk-live-5678"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "key_saved=1" in r.headers["location"]
    assert "sk-live-5678" in env_file.read_text(encoding="utf-8")
    page = client.get(r.headers["location"])
    assert "····5678" in page.text
    assert "sk-live-5678" not in page.text      # never echoed back


def test_key_post_empty_flashes_error(client, env_file):
    before = env_file.read_text(encoding="utf-8")
    r = client.post("/ops/llm-key", data={"api_key": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "key_error=" in r.headers["location"]
    assert env_file.read_text(encoding="utf-8") == before


def test_key_clear_route(client, env_file):
    ks.save_key("sk-x-9999")
    r = client.post("/ops/llm-key/clear", follow_redirects=False)
    assert r.status_code == 303
    assert not ks.status()["key_set"]
