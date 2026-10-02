"""Tests for the report browser: listing, viewing, promotion."""

from __future__ import annotations

import pytest

from jobscout.core import paths as core_paths
from jobscout.webapp import reports


@pytest.fixture()
def dirs(monkeypatch, tmp_path):
    """Point the report dirs at a tmp tree and return them."""
    digest = tmp_path / "digest"
    morning = tmp_path / "morning"
    digest.mkdir()
    morning.mkdir()
    monkeypatch.setattr(core_paths, "digest_dir", lambda: digest)
    monkeypatch.setattr(core_paths, "morning_reports_dir", lambda: morning)
    return digest, morning


def _write(directory, date: str, body: str) -> None:
    (directory / f"{date}.md").write_text(body, encoding="utf-8")


DAILY = """# jobscout digest — 2026-09-30

**3 new postings passed rules** (2 excluded by rules).

## New today — rule pass

| # | title | company |
|---|-------|---------|
| 1 | Quant Dev | Acme |
"""

MORNING = """# jobscout morning report — 2026-09-28

**12 steps · $0.0410 spent**

## Final summary

Found three quant shops.

## DB changes (auditable diff)

- +company DiaMonTech AG
- +company IOMED

## Tool log

### step 1 · `add_company`
- args: `{"name": "DiaMonTech AG"}`
- result: added

---

state: companies=38 (was 36), signals=12 (was 10), candidates=6 (was 4)
"""


def test_lists_dates_newest_first(dirs, conn):
    digest, morning = dirs
    _write(digest, "2026-09-28", DAILY)
    _write(digest, "2026-09-30", DAILY)
    _write(morning, "2026-09-29", MORNING)

    out = reports.list_reports(conn)
    assert [r["date"] for r in out["daily_reports"]] == ["2026-09-30", "2026-09-28"]
    assert [r["date"] for r in out["morning_reports"]] == ["2026-09-29"]


def test_ignores_non_date_files(dirs, conn):
    digest, _ = dirs
    _write(digest, "2026-09-30", DAILY)
    (digest / "notes.md").write_text("scratch", encoding="utf-8")
    out = reports.list_reports(conn)
    assert [r["date"] for r in out["daily_reports"]] == ["2026-09-30"]


def test_missing_dir_is_empty_not_an_error(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(core_paths, "digest_dir", lambda: tmp_path / "nope")
    assert reports.list_reports(conn)["daily_reports"] == []


def test_load_daily_report(dirs, conn):
    digest, _ = dirs
    _write(digest, "2026-09-30", DAILY)
    r = reports.load_report(conn, "daily", "2026-09-30")
    assert r["kind"] == "daily"
    assert "jobscout digest" in r["raw"]
    assert r["candidates"] == []       # no companies created that day
    assert r["companies_added"] is None


def test_load_morning_report_parses_db_changes(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    r = reports.load_report(conn, "morning", "2026-09-28")
    assert r["companies_added"] == [] and r["candidates"] is None
    assert "+company DiaMonTech AG" in r["db_changes"]
    assert "+company IOMED" in r["db_changes"]
    # the trailing state: line is not a change
    assert not any("state:" in d for d in r["db_changes"])


def test_morning_flag_when_candidates_unpromoted(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    conn.execute(
        "INSERT INTO companies (id, name, domain, tier, created_at) "
        "VALUES ('newco', 'NewCo', 'new.co', 'candidate', '2026-09-28 10:00:00')")
    conn.execute(
        "INSERT INTO companies (id, name, domain, tier, created_at) "
        "VALUES ('tierco', 'TierCo', 'tier.co', 'B', '2026-09-28 11:00:00')")
    conn.commit()

    out = reports.list_reports(conn)
    assert out["morning_reports"][0]["has_unpromoted"] is True

    r = reports.load_report(conn, "morning", "2026-09-28")
    names = {c["name"] for c in r["companies_added"]}
    assert names == {"NewCo", "TierCo"}
    # nothing suggested a tier here, so no advice is shown
    assert all(c["suggested_tier"] is None for c in r["companies_added"])


def test_suggested_tier_is_surfaced(dirs, conn):
    """The agent's recommendation rides along so the dropdown pre-selects."""
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    conn.execute(
        "INSERT INTO companies (id, name, domain, tier, suggested_tier, created_at) "
        "VALUES ('newco', 'NewCo', 'new.co', 'candidate', 'A', "
        "'2026-09-28 10:00:00')")
    conn.commit()
    r = reports.load_report(conn, "morning", "2026-09-28")
    assert r["companies_added"][0]["suggested_tier"] == "A"


def test_unpromoted_flag_clears_once_promoted(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    conn.execute(
        "INSERT INTO companies (id, name, domain, tier, created_at) "
        "VALUES ('newco', 'NewCo', 'new.co', 'candidate', '2026-09-28 10:00:00')")
    conn.commit()
    assert reports.list_reports(conn)["morning_reports"][0]["has_unpromoted"]

    conn.execute("UPDATE companies SET tier = 'A' WHERE id = 'newco'")
    conn.commit()
    assert not reports.list_reports(conn)["morning_reports"][0]["has_unpromoted"]


def test_bad_kind_and_bad_date_raise(dirs, conn):
    digest, _ = dirs
    _write(digest, "2026-09-30", DAILY)
    with pytest.raises(reports.ReportError):
        reports.load_report(conn, "weekly", "2026-09-30")
    with pytest.raises(reports.ReportError):
        reports.report_path("daily", "../../etc/passwd")
    with pytest.raises(reports.ReportError):
        reports.load_report(conn, "daily", "1999-01-01")


def test_signals_added(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    conn.execute(
        "INSERT INTO signals (id, company_id, kind, note, seen_at) "
        "VALUES ('s1', 'acme', 'funding', 'raised $17M', '2026-09-28 09:00:00')")
    conn.commit()
    r = reports.load_report(conn, "morning", "2026-09-28")
    assert r["signals_added"] == [
        {"kind": "funding", "note": "raised $17M", "company_name": "Acme"}]


# ── routes ────────────────────────────────────────────────────────────────────


def test_report_routes_render(client):
    r = client.get("/discovery/reports")
    assert r.status_code == 200
    assert "Run Reports" in r.text


def test_viewer_route_missing_report_is_graceful(client):
    r = client.get("/discovery/reports/daily/1999-01-01")
    assert r.status_code == 200
    assert "Report not found" in r.text


def test_viewer_route_rejects_unknown_kind(client):
    r = client.get("/discovery/reports/weekly/2026-09-30")
    assert r.status_code == 200
    assert "Report not found" in r.text


def test_discovery_page_mounts_the_browser(client):
    r = client.get("/discovery")
    assert 'hx-get="/discovery/reports"' in r.text
    assert 'id="report-tabs"' in r.text


# ── the Current tier column + un-tiered-first ordering ───────────────────────
# `tier` is nullable (the add-URL seeder writes NULL), so "no tier yet" is a
# real state and belongs at the top of the promotion table.


def _mk(conn, cid, name, date, tier, suggested=None):
    conn.execute(
        "INSERT INTO companies (id, name, domain, tier, suggested_tier, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cid, name, f"{cid}.com", tier, suggested, f"{date} 09:00:00"))
    conn.commit()


def test_un_tiered_companies_sort_first(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    # zzz is tiered and sorts last; aaa sorts before it alphabetically
    _mk(conn, "aaa-co", "AAA Co", "2026-09-28", "A", "A")
    _mk(conn, "zzz-co", "ZZZ Co", "2026-09-28", "B", "B")
    _mk(conn, "mmm-co", "MMM Co", "2026-09-28", None, None)
    names = [c["name"] for c in reports._companies_added(conn, "2026-09-28")]
    # the untiered one leads despite sorting last by name
    assert names[0] == "MMM Co"
    assert names[1:] == ["AAA Co", "ZZZ Co"]


def test_suggested_tier_does_not_drive_the_sort(dirs, conn):
    """A missing suggestion must NOT float a row up — only a missing tier does."""
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    _mk(conn, "aaa-co", "AAA Co", "2026-09-28", "A", None)   # tiered, no suggestion
    _mk(conn, "zzz-co", "ZZZ Co", "2026-09-28", "B", "B")    # tiered, suggested
    names = [c["name"] for c in reports._companies_added(conn, "2026-09-28")]
    assert names == ["AAA Co", "ZZZ Co"], "plain name order when both are tiered"


def test_candidate_tier_counts_as_tiered_not_un_tiered(dirs, conn):
    """'candidate' is a real tier; it must not be treated as un-tiered."""
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    _mk(conn, "zzz-cand", "ZZZ Cand", "2026-09-28", "candidate")
    _mk(conn, "aaa-none", "AAA None", "2026-09-28", None)
    names = [c["name"] for c in reports._companies_added(conn, "2026-09-28")]
    assert names == ["AAA None", "ZZZ Cand"]


def test_all_un_tiered_still_sorted_by_name(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    for cid, nm in (("c-bee", "Bee"), ("a-aye", "Aye"), ("c-see", "See")):
        _mk(conn, cid, nm, "2026-09-28", None)
    names = [c["name"] for c in reports._companies_added(conn, "2026-09-28")]
    assert names == ["Aye", "Bee", "See"]


def test_viewer_shows_the_stored_tier_and_flags_un_tiered(dirs, conn):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    _mk(conn, "live-co", "Live Co", "2026-09-28", "A", "B")
    _mk(conn, "cand-co", "Cand Co", "2026-09-28", "candidate")
    _mk(conn, "bare-co", "Bare Co", "2026-09-28", None)
    r = _view(dirs)
    assert r.status_code == 200
    assert ">Current<" in r.text, "the Current tier column header is missing"
    assert "already on the watchlist at tier A" in r.text
    assert "not yet promoted to a tier" in r.text
    assert "un-tiered" in r.text, "a NULL tier must read as un-tiered, not a dash"


def _view(dirs):
    import sqlite3

    from fastapi.testclient import TestClient

    import jobscout.core.db as _db
    from jobscout.webapp.routes import create_app

    path = dirs[1].parent / "test.db"

    def fake_connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    _db.connect = fake_connect
    return TestClient(create_app()).get("/discovery/reports/morning/2026-09-28")
