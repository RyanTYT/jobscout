
"""test_routes_applications — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations

import sqlite3

from jobscout.core import db
from tests.conftest import _seed_packet


def test_applications_page_shows_apply_ui(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.get("/applications")
    assert r.status_code == 200
    assert 'name="pk"' in r.text                       # selection checkboxes
    assert "apply to selected" in r.text
    assert "select all" in r.text
    assert "<th scope=\"col\">details</th>" in r.text  # readiness column
    assert "Apply runs" in r.text                      # live run log
    assert "0 selected" in r.text                      # JS-free default count



def test_applications_details_column_reads_fill_sheet(client, db_file, tmp_path):
    pkt_dir = _seed_packet(db_file, tmp_path)
    (pkt_dir / "fill_sheet.yaml").write_text(
        "fields:\n"
        "- {label: Email, canonical_key: email, required: true,"
        " value: null, confidence: missing}\n")
    r = client.get("/applications")
    assert "1 missing" in r.text
    (pkt_dir / "fill_sheet.yaml").write_text(
        "fields:\n"
        "- {label: Email, canonical_key: email, required: true,"
        " value: x@y, confidence: exact}\n")
    r = client.get("/applications")
    assert "all known" in r.text



def test_applications_apply_launches_and_redirects(client, db_file, monkeypatch):
    _seed_packet(db_file, db_file.parent)
    launched = {}

    def fake_launch(conn, ids):
        launched["ids"] = ids
        db.record_apply_run(conn, packet_id=ids[0], mode="assisted",
                            status="opened", detail="test")
        return {"automated": [], "assisted": ids, "note": []}

    from jobscout.webapp.runners import apply as apply_mod
    monkeypatch.setattr(apply_mod, "launch_apply", fake_launch)
    r = client.post("/applications/apply", data={"pk": "pk_w1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "/applications?applied=1" in r.headers["location"]
    assert launched["ids"] == ["pk_w1"]
    r2 = client.get(r.headers["location"])
    assert "browser open" in r2.text or "Apply runs" in r2.text



def test_applications_apply_error_redirects(client, db_file, monkeypatch):
    _seed_packet(db_file, db_file.parent)

    from jobscout.webapp.runners import apply as apply_mod
    def boom(conn, ids):
        raise apply_mod.ApplyError("nope")

    monkeypatch.setattr(apply_mod, "launch_apply", boom)
    r = client.post("/applications/apply", data={"pk": "pk_w1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=nope" in r.headers["location"]



def test_applications_runs_partial(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    conn = sqlite3.connect(db_file)
    db.record_apply_run(conn, packet_id="pk_w1", mode="automated",
                        status="launched", detail="headed filler run")
    conn.close()
    r = client.get("/applications/runs")
    assert r.status_code == 200
    assert "Apply runs" in r.text
    assert "launched" in r.text
    assert "every 3s" in r.text                        # polls while live





def test_applications_rows_carry_row_links(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.get("/applications")
    assert 'data-row-link="/packet/pk_w1"' in r.text




# ── follow-through: manual application-state tracking ────────────────────


def test_packet_status_updates_through_lifecycle(client, db_file, tmp_path):
    """applied → interviewing → offer → rejected via the row select."""
    _seed_packet(db_file, tmp_path)
    import sqlite3

    for status in ("applied", "interviewing", "offer", "rejected"):
        r = client.post("/packet/pk_w1/status", data={"status": status},
                        headers={"HX-Request": "true"},
                        follow_redirects=False)
        assert r.status_code == 200, f"{status}: {r.status_code}"
        assert "id=\"pk-row-pk_w1\"" in r.text          # the swapped row
        c = sqlite3.connect(db_file)
        cur = c.execute("SELECT status FROM packets WHERE id = 'pk_w1'").fetchone()
        c.close()
        assert cur[0] == status


def test_packet_status_select_renders(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.get("/applications")
    assert 'hx-post="/packet/pk_w1/status"' in r.text
    assert "interview in progress" in r.text
    assert 'value="offer"' in r.text and 'value="rejected"' in r.text


def test_packet_bad_status_rejected(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.post("/packet/pk_w1/status", data={"status": "nonsense"},
                    follow_redirects=False)
    assert r.status_code == 303                        # not written; redirects
    import sqlite3

    c = sqlite3.connect(db_file)
    cur = c.execute("SELECT status FROM packets WHERE id = 'pk_w1'").fetchone()
    c.close()
    assert cur[0] == "packet:ready"


def test_new_stages_render_groups(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    import sqlite3

    c = sqlite3.connect(db_file)
    c.execute("UPDATE packets SET status = 'interviewing' WHERE id = 'pk_w1'")
    c.commit()
    c.close()
    r = client.get("/applications")
    assert "Interview in progress" in r.text
    assert "pk_w1" in r.text


# ── timeline: applied stamps, manual entries, quiet detection ──────────────


def test_set_status_stamps_and_records_event(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    client.post("/packet/pk_w1/status", data={"status": "applied"},
                headers={"HX-Request": "true"})
    import sqlite3

    c = sqlite3.connect(db_file)
    row = c.execute("SELECT applied_at FROM packets WHERE id = 'pk_w1'"
                    ).fetchone()
    events = c.execute("SELECT kind, title FROM application_events"
                       " WHERE packet_id = 'pk_w1'").fetchall()
    c.close()
    assert row[0] is not None                    # stamped
    assert ("applied", "state → applied") in events  # auto timeline event


def test_manual_timeline_entry_roundtrip(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.post("/packet/pk_w1/events", data={
        "kind": "oa", "event_date": "2026-10-02",
        "title": "CodeSignal OA", "notes": "2 mediums + SQL question",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/packet/pk_w1")
    assert "CodeSignal OA" in page.text
    assert "2 mediums + SQL" in page.text
    assert "Timeline" in page.text


def test_timeline_empty_rejected(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.post("/packet/pk_w1/events", data={
        "kind": "note", "event_date": "", "title": "", "notes": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_board_shows_applied_age_and_quiet(client, db_file, tmp_path):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    _seed_packet(db_file, tmp_path)
    c = sqlite3.connect(db_file)
    old = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%d")
    c.execute("UPDATE packets SET status = 'applied', applied_at = ?,"
              " updated_at = ? WHERE id = 'pk_w1'", (old, old))
    c.commit()
    c.close()
    r = client.get("/applications")
    assert "20d" in r.text
    assert "quiet" in r.text                    # the stale badge
    assert "follow up?" in r.text


def test_follow_up_route_starts_draft(client, db_file, tmp_path,
                                      monkeypatch):
    _seed_packet(db_file, tmp_path)
    import sqlite3

    c = sqlite3.connect(db_file)
    c.execute("UPDATE packets SET status = 'applied',"
              " applied_at = '2026-09-01' WHERE id = 'pk_w1'")
    c.commit()
    c.close()
    started = {}
    monkeypatch.setattr(
        "jobscout.webapp.routers.applications.outreach_runner"
        if False else "jobscout.webapp.runners.outreach.start_follow_up",
        lambda pid: started.update(pid=pid))
    r = client.post("/packet/pk_w1/follow-up", follow_redirects=False)
    assert r.status_code == 303
    assert started["pid"] == "pk_w1"
    r2 = client.get("/packet/pk_w1/follow-up-status")
    assert r2.status_code == 200




# ── OA/interview search + per-company history ─────────────────────────────


def test_event_search_finds_oa_notes(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    import sqlite3

    c = sqlite3.connect(db_file)
    c.execute("UPDATE packets SET status = 'applied',"
              " applied_at = '2026-09-01' WHERE id = 'pk_w1'")
    c.execute("UPDATE postings SET company_id = 'acme', title = 'Engineer II'"
              " WHERE id = 'p_int_1'")
    db.record_app_event(c, packet_id="pk_w1", kind="oa",
                        event_date="2026-09-15", title="CodeSignal",
                        notes="SQL join question + two mediums")
    c.commit()
    c.close()
    r = client.get("/applications?q=SQL")
    assert "CodeSignal" in r.text
    assert "SQL join question" in r.text
    assert "acme" in r.text.lower()
    r2 = client.get("/applications?q=nomatchterm")
    assert "no timeline entries match" in r2.text


def test_company_history_card(client):


    # company detail needs a real watchlist entry (jane-street in seed)
    r = client.get("/companies/jane-street")
    assert r.status_code == 200
    assert "Application history" in r.text
    assert "no applications recorded" in r.text or "packet" in r.text.lower()


def test_company_history_lists_packets(client, db_file):
    import sqlite3

    c = sqlite3.connect(db_file)
    c.execute("INSERT INTO packets (id, posting_id, status, applied_at,"
              " decided_at) VALUES ('pk_w2', 'p_int_1', 'rejected',"
              " '2026-09-01', '2026-09-20')")
    c.execute("UPDATE postings SET company_id = 'jane-street'"
              " WHERE id = 'p_int_1'")
    c.commit()
    c.close()
    r = client.get("/companies/jane-street")
    assert "rejected" in r.text
    assert "2026-09-01" in r.text and "2026-09-20" in r.text


def test_pipeline_knob_saves_and_flashes(client):
    r = client.post("/discovery/pipeline", data={
        "ats_boards": "1", "careers_crawl": "1", "rss": "1",
        "job_sites": "1", "cse_queries": "10",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "sweep_saved=1" in r.headers["location"]
    page = client.get("/discovery")
    assert "Daily sweep" in page.text
    assert 'name="job_sites"' in page.text


def test_pipeline_knob_rejects_bad_cse(client):
    r = client.post("/discovery/pipeline", data={
        "ats_boards": "1", "careers_crawl": "1", "rss": "1",
        "job_sites": "1", "cse_queries": "abc",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_sweep_sites_registered():
    from jobscout.sources.postings.sites import SITES

    assert "mycareersfuture" in SITES


def test_mcf_search_parses(monkeypatch):
    from jobscout.sources.postings import sites as sites_mod

    class FakeResp:
        text = '{"results": []}'

        def json(self):
            # the VERIFIED live API shape (v2/jobs, 2026-09-29)
            return {"results": [
                {"title": "Software Engineer",
                 "postedCompany": {"name": "SG Corp"},
                 "uuid": "b31",
                 "metadata": {"jobPostId": "MCF-123"},
                 "description": "<p>Build <b>systems</b></p>"},
                {"title": None, "postedCompany": {}},   # skipped: no title
            ]}
        status_code = 200

    class FakeClient:
        def get(self, url, params=None):
            class R(FakeResp):
                pass
            return R()

    out = sites_mod.mcf_search("engineer", "Singapore", FakeClient())
    assert len(out) == 1
    assert out[0].company == "SG Corp"
    assert out[0].url.endswith("MCF-123")
    assert "Build systems" in out[0].description
    assert out[0].source == 'site:mycareersfuture'
