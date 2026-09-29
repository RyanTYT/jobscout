"""Apply-launcher tests: tiling, profile building, classification, the
sidecar apply path (fake client), and the assisted open path (no real
browser is ever launched in tests)."""

from __future__ import annotations

import sqlite3
import textwrap

import pytest

from jobscout.core import db
from jobscout.webapp import apply as apply_mod

# ── test data ───────────────────────────────────────────────────────────────

SEED = textwrap.dedent("""
INSERT INTO companies (id, name, domain, tier, non_ats) VALUES
  ('acme', 'Acme', 'acme.com', 'A', 0);

INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      rule_pass, status, first_seen, last_seen, content_hash)
VALUES
  ('p_1', 'ats:greenhouse:acme', 'acme', 'https://boards.greenhouse.io/acme/jobs/1',
   'h1', 'Senior Engineer', 1, 'packet:ready',
   '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', 'c1'),
  ('p_2', 'careers:jsonld:acme', 'acme', 'https://acme.com/careers/apply-2',
   'h2', 'Staff Engineer', 1, 'packet:ready',
   '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', 'c2');

INSERT INTO packets (id, posting_id, status, dir)
VALUES
  ('pk_1', 'p_1', 'ready', 'DIR1'),
  ('pk_2', 'p_2', 'ready', 'DIR2');
""")

FILL_SHEET = textwrap.dedent("""
fields:
- label: Full Name
  canonical_key: full_name
  required: true
  value: Jane Doe
  value_source: identity.full_name
  confidence: exact
- label: Email
  canonical_key: email
  required: true
  value: jane@example.com
  value_source: identity.email
  confidence: exact
- label: Salary Expectation
  canonical_key: salary_expectation
  required: true
  value: null
  value_source: ''
  confidence: missing
""")


class FakeSidecar:
    """Stands in for SidecarClient — records the apply request, streams
    events on demand."""

    def __init__(self, hostnames=("boards.greenhouse.io",)):
        self.hostnames = list(hostnames)
        self.applied: list[dict] = []
        self._tracked_apply_ids: list[str] = []
        self._pending: dict[str, list] = {}

    def get_fillers(self) -> list[dict]:
        return [{"id": "greenhouse", "handles_hostnames": self.hostnames,
                 "health": "healthy"}]

    def apply_jobs_by_payload(self, jobs, profile, settings, *, timeout=60.0):
        self.applied.append({"jobs": jobs, "profile": profile,
                             "settings": settings})
        req_id = f"req-{len(self.applied)}"
        self._pending[req_id] = [
            {"id": req_id, "result": {"id": "rec-1", "job": {"id": jobs[0]["id"]},
                                      "status": "filling"}},
            {"id": req_id, "result": {"id": "rec-1", "job": {"id": jobs[0]["id"]},
                                      "status": "paused",
                                      "pauseReason": "cover_letter_required"}},
        ]
        return {"id": req_id, "msg": "started", "event_type": "update"}

    def drain_events(self, req_id):
        return self._pending.pop(req_id, [])


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "t.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    c.executescript(SEED.replace("DIR1", str(tmp_path / "pk1"))
                    .replace("DIR2", str(tmp_path / "pk2")))
    c.commit()
    (tmp_path / "pk1").mkdir()
    (tmp_path / "pk1" / "fill_sheet.yaml").write_text(FILL_SHEET)
    (tmp_path / "pk1" / "resume.md").write_text("# Jane Doe resume")
    return c


# ── tiling ──────────────────────────────────────────────────────────────────


def test_tile_grid_single_window(monkeypatch):
    monkeypatch.setattr(apply_mod, "screen_size", lambda: (1440, 900))
    grid = apply_mod.tile_grid(1)
    assert len(grid) == 1
    assert grid[0]["width"] > 1000 and grid[0]["height"] > 800


def test_tile_grid_four_windows(monkeypatch):
    monkeypatch.setattr(apply_mod, "screen_size", lambda: (1440, 900))
    grid = apply_mod.tile_grid(4)
    assert len(grid) == 4
    assert len({(g["x"], g["y"]) for g in grid}) == 4       # distinct slots
    assert all(g["x"] + g["width"] <= 1440 for g in grid)   # on screen
    assert all(g["y"] + g["height"] <= 900 for g in grid)


def test_screen_size_fallback(monkeypatch):
    def fake_run(*a, **kw):
        raise OSError("no osascript")

    monkeypatch.setattr(apply_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(apply_mod, "_SCREEN_CACHE", None)
    assert apply_mod.screen_size() == (1440, 900)


# ── readiness + profile building ─────────────────────────────────────────────


def test_sheet_missing_count(conn):
    pk = db.get_packet(conn, "pk_1")
    assert apply_mod.sheet_missing_count(pk) == 1           # salary
    assert apply_mod.sheet_missing_count({"dir": None}) is None


def test_build_profile_from_fill_sheet(conn):
    pk = db.get_packet(conn, "pk_1")
    packets = [{"row": pk, "url": "https://x", "host": "x"}]
    profile = apply_mod._build_profile(conn, packets)
    assert profile["firstName"] == "Jane"
    assert profile["lastName"] == "Doe"
    assert profile["email"] == "jane@example.com"
    assert profile["resumePath"].endswith("resume.md")


# ── launch: automated + assisted split ───────────────────────────────────────


def test_launch_apply_automated_and_assisted(conn, monkeypatch, tmp_path):
    fake = FakeSidecar()
    monkeypatch.setattr(apply_mod, "get_sidecar", lambda: fake)
    opened = []
    monkeypatch.setattr(apply_mod, "open_assisted",
                        lambda url, rect: opened.append((url, rect)) or True)
    monkeypatch.setattr(apply_mod, "tile_grid",
                        lambda n: [{"x": 0, "y": 0, "width": 1, "height": 1}] * n)

    result = apply_mod.launch_apply(conn, ["pk_1", "pk_2"])

    # greenhouse URL → automated; acme.com careers → assisted
    assert result["automated"] == ["pk_1"]
    assert result["assisted"] == ["pk_2"]
    assert len(fake.applied) == 1
    payload = fake.applied[0]
    assert payload["jobs"][0]["id"] == "pk_1"
    assert payload["jobs"][0]["applyHostname"] == "boards.greenhouse.io"
    assert payload["settings"]["pauseOnUncertainty"] is True
    assert payload["settings"]["windows"][0]["width"] == 1
    assert payload["profile"]["email"] == "jane@example.com"
    assert len(opened) == 1 and opened[0][0].endswith("apply-2")

    rows = conn.execute("SELECT packet_id, mode, status FROM apply_runs"
                        " ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("pk_1", "automated", "launched"),
        ("pk_2", "assisted", "opened"),
    ]


def test_launch_apply_without_sidecar_all_assisted(conn, monkeypatch):
    monkeypatch.setattr(apply_mod, "get_sidecar", lambda: None)
    opened = []
    monkeypatch.setattr(apply_mod, "open_assisted",
                        lambda url, rect: opened.append((url, rect)) or True)

    result = apply_mod.launch_apply(conn, ["pk_1", "pk_2"])

    assert result["automated"] == []
    assert sorted(result["assisted"]) == ["pk_1", "pk_2"]
    assert result["note"]                     # explains the fallback


def test_launch_apply_rejects_empty(conn):
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.launch_apply(conn, ["pk_missing"])


def test_launch_apply_no_url_records_failure(conn, monkeypatch):
    conn.execute("UPDATE postings SET url = '' WHERE id = 'p_2'")
    conn.commit()
    monkeypatch.setattr(apply_mod, "get_sidecar", lambda: None)
    monkeypatch.setattr(apply_mod, "open_assisted", lambda u, r: True)
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.launch_apply(conn, ["pk_2"])   # nothing left to launch
    row = conn.execute("SELECT status, detail FROM apply_runs").fetchone()
    assert row["status"] == "failed"


# ── event draining → statuses ────────────────────────────────────────────────


def test_refresh_runs_updates_statuses(conn, monkeypatch):
    fake = FakeSidecar()
    monkeypatch.setattr(apply_mod, "get_sidecar", lambda: fake)
    monkeypatch.setattr(apply_mod, "open_assisted", lambda u, r: True)
    monkeypatch.setattr(apply_mod, "_SIDE", fake)   # refresh drains the singleton
    apply_mod.launch_apply(conn, ["pk_1"])
    assert fake._pending                       # events waiting

    apply_mod.refresh_runs(conn)

    row = conn.execute("SELECT status, detail FROM apply_runs").fetchone()
    assert row["status"] == "paused"
    assert "cover_letter_required" in row["detail"]


def test_refresh_runs_noop_without_sidecar(conn, monkeypatch):
    monkeypatch.setattr(apply_mod, "get_sidecar", lambda: None)
    apply_mod.refresh_runs(conn)               # must not raise


# ── assisted window command ─────────────────────────────────────────────────


def test_open_assisted_chrome(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(apply_mod, "_chrome_path", lambda: "/Applications/Google Chrome.app")
    monkeypatch.setattr(apply_mod.subprocess, "Popen", FakePopen)
    tiled = apply_mod.open_assisted("https://acme.com/apply",
                                    {"x": 10, "y": 20, "width": 640, "height": 400})
    assert tiled
    assert calls[0][0] == "open"
    assert "--window-position=10,20" in calls[0]
    assert "--window-size=640,400" in calls[0]
    assert calls[0][-1] == "https://acme.com/apply"


# ── packaged-app relocation (Tauri shell sets these env vars) ───────────────


def test_jobscout_home_relocates_runtime(monkeypatch, tmp_path):
    from jobscout.core import paths

    home = tmp_path / "appdata"
    monkeypatch.setattr(paths.os, "environ", {"JOBSCOUT_HOME": str(home)})
    assert paths.repo_root() == home.resolve()
    assert paths.db_path() == home.resolve() / "var" / "data" / "jobscout.db"
    assert paths.applications_dir() == home.resolve() / "var" / "applications"


def test_jobscout_home_unset_uses_repo(monkeypatch):
    from jobscout.core import paths

    monkeypatch.delenv("JOBSCOUT_HOME", raising=False)
    root = paths.repo_root()
    assert (root / "PLAN.md").is_file()      # the real checkout resolves
    assert root / "pyproject.toml"


def test_sidecar_env_override(monkeypatch, tmp_path):
    from jobscout.sidecar import SidecarClient

    fake = tmp_path / "bundled-sidecar.js"
    fake.write_text("// sidecar")
    monkeypatch.setenv("JOBSCOUT_SIDECAR_BIN", str(fake))
    assert SidecarClient._default_path() == fake
