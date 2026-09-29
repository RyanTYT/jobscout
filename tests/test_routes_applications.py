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


