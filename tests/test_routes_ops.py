"""test_routes_ops — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations


def test_ops_spend_partial_polls_while_running(client):
    from jobscout.webapp.runners import agent_runner as _ar

    _ar._state.update(running=True, out="", error="")
    try:
        r = client.get("/ops/spend")
        assert r.status_code == 200
        assert "every 3s" in r.text
        assert "live" in r.text
    finally:
        _ar._state.update(running=False, out="", error="")



def test_credentials_card_replace_and_remove_semantics(client, db_file):
    r = client.get("/ops")
    assert "Replace the saved key" in r.text
    assert "replaces" in r.text
    # remove button appears only when a key is saved
    assert "remove key" not in r.text
    import pathlib

    from jobscout.webapp.stores import key_store as _ks

    fake = pathlib.Path("/tmp/_fake.env")
    fake.write_text("JOBSCOUT_LLM_API_KEY=sk-zzz\n", encoding="utf-8")
    from jobscout.core import paths as _cp
    orig_store, orig_cp = _ks._env_path, _cp.env_path
    _ks._env_path = lambda: fake
    from jobscout.core import paths as _cp
    _cp.env_path = lambda: fake     # status() reads via load_env
    try:
        r = client.get("/ops")
        assert "remove key" in r.text
        assert "data-confirm-prompt" in r.text
        assert "key saved" in r.text and "zzz" in r.text
    finally:
        _ks._env_path, _cp.env_path = orig_store, orig_cp
        fake.unlink(missing_ok=True)




def test_open_url_endpoint_rejects_non_http(client):
    r = client.post("/open-url", data={"url": "file:///etc/passwd"})
    assert r.status_code == 200
    assert r.json()["error"]
    r2 = client.post("/open-url", data={"url": "javascript:alert(1)"})
    assert r2.json()["error"]



def test_open_url_endpoint_opens_default_browser(client, monkeypatch):
    import subprocess

    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = client.post("/open-url", data={"url": "https://acme.com/careers"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [["open", "https://acme.com/careers"]]











def test_htmx_failure_toast_handler_present(client):
    r = client.get("/static/js/app.js")
    assert "htmx:responseError" in r.text
    assert "htmx:sendError" in r.text
    assert "request failed" in r.text


def test_source_health_card_uses_the_last_daily_run(client, db_file):
    """The card is labelled "last daily run" — it must not show the agent run.

    The agent run starts after the 06:30 sweep, so it is always the newer row;
    reading runs[0] (newest of any kind) made this card permanently report the
    agent's numbers under a "source health" heading.
    """
    import json
    import sqlite3

    conn = sqlite3.connect(db_file)
    try:
        conn.execute("DELETE FROM runs")
        conn.execute(
            "INSERT INTO runs (kind, started, stats) VALUES ('daily', '2026-10-02 06:30:00', ?)",
            (json.dumps({"companies": 3, "postings_seen": 187, "errors": 0}),),
        )
        conn.execute(
            "INSERT INTO runs (kind, started, stats) VALUES ('agent', '2026-10-02 07:00:00', ?)",
            (json.dumps({"companies": 1, "postings_seen": 0, "steps": 31}),),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/ops")
    assert r.status_code == 200
    assert "Source health (last daily run)" in r.text
    assert "187" in r.text          # the sweep's postings_seen, not the agent's 0
