"""Hunting-profile editor: line patches with comment preservation, pydantic
validation + rollback, and the /discovery/targeting route."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import config as core_config
from jobscout.webapp import targeting_store as ts

PROFILE = (Path(__file__).resolve().parents[1]
           / "config" / "profile.yaml").read_text(encoding="utf-8")


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    (d / "profile.yaml").write_text(PROFILE, encoding="utf-8")
    for extra in ("settings.yaml", "models.yaml", "watchlist.yaml"):
        src = Path(__file__).resolve().parents[1] / "config" / extra
        if src.is_file():
            (d / extra).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    from jobscout.core import paths as core_paths
    monkeypatch.setattr(core_paths, "config_dir", lambda: d)
    return d


def _save(**over):
    kw = dict(seniorities=["junior"], primary_locations="Singapore",
              other_locations="Hong Kong, Tokyo", roles="backend engineer",
              stack="rust, python", remote_preference="hybrid",
              remote_allowed=True)
    kw.update(over)
    return ts.save(**kw)


# ── store ────────────────────────────────────────────────────────────────────


def test_save_roundtrip_and_version_bump(cfg_dir):
    result = _save()
    t = core_config.load_profile().target
    assert t.seniorities == ["junior"]
    assert t.primary_locations == ["Singapore"]
    assert t.locations == ["Singapore", "Hong Kong", "Tokyo"]
    assert t.remote.allowed is True
    today = result["profile_version"].split(".")[0]
    assert result["profile_version"].startswith(f"{today}.")


def test_save_preserves_other_keys_and_comments(cfg_dir):
    before = (cfg_dir / "profile.yaml").read_text(encoding="utf-8")
    assert "Distilled scoring rubric" in before        # header comment
    assert "quant: 0.7" in before                      # weighting block
    _save()
    after = (cfg_dir / "profile.yaml").read_text(encoding="utf-8")
    assert "Distilled scoring rubric" in after         # header survives
    assert "quant: 0.7" in after                       # weighting untouched
    assert "market-data" in after                      # domains untouched


def test_save_clears_to_any_level_any_location(cfg_dir):
    _save(seniorities=[], primary_locations="", other_locations="")
    t = core_config.load_profile().target
    assert t.seniorities == []
    assert t.locations == []


def test_invalid_preference_rejected_file_untouched(cfg_dir):
    before = (cfg_dir / "profile.yaml").read_bytes()
    with pytest.raises(ts.TargetingError):
        _save(remote_preference="sometimes")
    assert (cfg_dir / "profile.yaml").read_bytes() == before


def test_unrecognised_structure_raises(cfg_dir, monkeypatch):
    (cfg_dir / "profile.yaml").write_text("profile_version: 'x'\ntarget: 42\n")
    with pytest.raises(ts.TargetingError):
        _save()


# ── route ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, cfg_dir, tmp_path):
    import sqlite3

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


def test_targeting_post_redirects_and_persists(client, cfg_dir):
    r = client.post("/discovery/targeting", data={
        "seniorities": ["junior", "senior"],
        "primary_locations": "Singapore",
        "other_locations": "Hong Kong",
        "roles": "backend engineer",
        "stack": "rust",
        "remote_preference": "remote",
        "remote_allowed": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]
    t = core_config.load_profile().target
    assert t.seniorities == ["junior", "senior"]
    assert t.remote.preference == "remote"
    page = client.get(r.headers["location"])
    assert "Hunting profile saved" in page.text


def test_targeting_post_error_flashes(client, cfg_dir):
    before = (cfg_dir / "profile.yaml").read_bytes()
    r = client.post("/discovery/targeting", data={
        "remote_preference": "nonsense",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert (cfg_dir / "profile.yaml").read_bytes() == before


def test_discovery_page_renders_editor(client):
    r = client.get("/discovery")
    assert r.status_code == 200
    assert 'name="seniorities"' in r.text
    assert 'name="primary_locations"' in r.text
    assert 'name="other_locations"' in r.text
    assert "save target" in r.text
