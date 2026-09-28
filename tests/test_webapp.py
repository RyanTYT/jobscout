"""Webapp tests: ui view-model, db filters, and live route behavior.

Route tests run the real FastAPI app against a throwaway file DB via
monkeypatched db.connect — every page renders with seeded data, filters
and pagination behave, and the HTMX partial contract holds.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import db
from jobscout.webapp import ui
from jobscout.webapp.routes import create_app

# ── test data ───────────────────────────────────────────────────────────────

SEED = textwrap.dedent("""
INSERT INTO companies (id, name, domain, tier, non_ats, career_url)
VALUES
  ('acme',  'Acme',    'acme.com',  'A', 0, NULL),
  ('globex', 'Globex', 'globex.com', 'B', 1, 'https://globex.com/careers');

-- 60 new postings for Acme (pagination fodder), varying level/location
WITH RECURSIVE seq(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 60)
INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, remote, rule_pass, status,
                      first_seen, last_seen, content_hash)
SELECT
  'p_new_' || printf('%03d', i),
  CASE WHEN i % 2 = 0 THEN 'ats:greenhouse:acme' ELSE 'careers:jsonld:globex' END,
  CASE WHEN i % 2 = 0 THEN 'acme' ELSE 'globex' END,
  'https://x/' || i, printf('%064d', i),
  'Software Engineer ' || i,
  CASE WHEN i % 3 = 0 THEN 'New York, NY' ELSE 'Singapore' END,
  CASE WHEN i % 4 = 0 THEN 'senior' ELSE NULL END,
  CASE WHEN i % 5 = 0 THEN 1 ELSE 0 END,
  1, 'new', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', printf('%064x', i)
FROM seq;

INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, rule_pass, status, first_seen,
                      last_seen, content_hash)
VALUES
  ('p_int_1', 'ats:greenhouse:acme', 'acme', 'https://x/int1',
   'i1', 'Senior Quant', 'New York, NY', 'senior', 1, 'interested',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'i1x'),
  ('p_dis_1', 'ats:greenhouse:acme', 'acme', 'https://x/dis1',
   'd1', 'Junior Analyst', 'London', 'junior', 1, 'dismissed',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'd1x'),
  ('p_rule_1', 'ats:greenhouse:acme', 'acme', 'https://x/rule1',
   'r1', 'Chef', 'Berlin', NULL, 0, 'new', '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'r1x');
UPDATE companies SET ats_tokens = '{"greenhouse":"acme"}' WHERE id = 'acme';
""")


@pytest.fixture()
def db_file(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    conn.executescript(SEED)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(monkeypatch, db_file):
    def fake_connect():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "init_db", lambda: db_file)
    app = create_app()
    return TestClient(app)


# ── ui view-model (pure logic) ──────────────────────────────────────────────


def test_filters_from_query_drops_unknown():
    f = ui.InboxFilters.from_query(status="bogus", sort="nope", tier="Z")
    assert f.status == "new"       # default retained
    assert f.sort == "score"
    assert f.tier == ""


def test_filters_query_string_roundtrip():
    f = ui.InboxFilters.from_query(
        status="interested", level="senior", q="rust", page="3",
    )
    qs = f.query_string()
    assert "status=interested" in qs and "level=senior" in qs
    assert "page=3" not in qs          # page is not filter state
    assert f.is_filtered()


def test_filters_defaults_not_filtered():
    assert not ui.InboxFilters().is_filtered()
    assert ui.InboxFilters().query_string() == ""


def test_pagination_window_small():
    pg = ui.Pagination(total=60, page=1, page_size=25)
    assert pg.pages == 3
    assert pg.window == [1, 2, 3]
    assert pg.offset == 0 and (pg.start, pg.end) == (1, 25)


def test_pagination_window_gaps():
    pg = ui.Pagination(total=500, page=10, page_size=25)
    assert pg.pages == 20
    assert pg.window == [1, None, 8, 9, 10, 11, 12, None, 20]


def test_pagination_clamps_overshoot():
    pg = ui.Pagination(total=60, page=99, page_size=25)
    assert pg.clamped_page == 3
    assert pg.offset == 50


def test_pagination_size_whitelist():
    pg = ui.Pagination.from_query(100, "2", "777", ui.InboxFilters())
    assert pg.page_size == ui.DEFAULT_PAGE_SIZE


# ── db filtering layer ──────────────────────────────────────────────────────


@pytest.fixture()
def conn(db_file):
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_count_matches_list_across_pages(conn):
    kw = ui.InboxFilters().as_kwargs()
    total = db.count_postings(conn, **kw)
    gathered = 0
    for page in range(0, 10):
        rows = db.list_postings(conn, **kw, limit=25, offset=page * 25)
        gathered += len(rows)
        if len(rows) < 25:
            break
    assert gathered == total == 60   # 60 new rule-pass postings


def test_level_filter_including_unknown(conn):
    assert db.count_postings(conn, level="senior") == 15    # new-status only
    assert db.count_postings(conn, level="unknown") == 45   # NULL seniority


def test_status_and_company_filter(conn):
    assert db.count_postings(conn, status="interested") == 1
    assert db.count_postings(conn, company="globex") == 30


def test_source_filter(conn):
    assert db.count_postings(conn, source="greenhouse") == 30  # new-status only
    assert db.count_postings(conn, source="careers") == 30


def test_remote_filter(conn):
    assert db.count_postings(conn, remote="1") == 12


def test_filter_options(conn):
    opts = db.filter_options(conn)
    assert any(v == "senior" for v, _, _ in opts["levels"])
    assert ("acme", "Acme", 32) in opts["companies"]
    assert {v for v, _, _ in opts["sources"]} == {"greenhouse", "careers"}


# ── live routes ─────────────────────────────────────────────────────────────


def test_inbox_renders_with_results(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="results"' in r.text
    assert "Inbox" in r.text


def test_inbox_hx_returns_partial_only(client):
    r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="results"' in r.text
    assert "nav__link" not in r.text          # no sidebar in partial
    assert "<html" not in r.text               # not a full document


def test_pagination_second_page(client):
    r = client.get("/?page=2&page_size=25")
    assert r.status_code == 200
    assert "26–50 of 60" in r.text.replace("&ndash;", "–")


def test_filter_narrows_results(client):
    r = client.get("/?level=senior&status=interested")
    assert r.status_code == 200
    assert "1 postings" in r.text
    assert "Senior Quant" in r.text


def test_location_search(client):
    r = client.get("/?location=Singapore")
    assert "40 postings" in r.text


def test_all_postings_flag_includes_rule_excluded(client):
    r = client.get("/?all_postings=1")
    assert "of 61" in r.text                    # 60 + the excluded Chef


def test_status_post_swaps_row_and_sets_toast(client):
    r = client.post(
        "/posting/p_new_001/status",
        data={"status": "interested"},
        headers={"HX-Request": "true", "HX-Target": "#row-p_new_001"},
    )
    assert r.status_code == 200
    assert r.text.strip().startswith('<tr id="row-p_new_001"')
    assert "X-Toast" in r.headers
    # persisted?
    r2 = client.get("/?status=interested")
    assert "Software Engineer 1</a>" in r2.text


def test_all_pages_render(client):
    for path in ("/companies", "/discovery", "/applications", "/ops"):
        assert client.get(path).status_code == 200, path
    r = client.get("/posting/p_int_1")
    assert r.status_code == 200
    assert "Senior Quant" in r.text


def test_companies_shows_dark_pool_and_boards(client):
    text = client.get("/companies").text
    assert "Acme" in text and "Globex" in text
    assert "greenhouse" in text
    assert "dark-pool" in text


def test_unknown_posting_redirects(client):
    r = client.get("/posting/nope", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ── linkify / signal kinds / sorting (ui layer) ─────────────────────────────


def test_linkify_extracts_url():
    out = ui.linkify("new job URL in sitemap: https://acme.com/jobs/fpga")
    assert '<a class="signal__url"' in out
    assert 'href="https://acme.com/jobs/fpga"' in out
    assert "acme.com/jobs/fpga</a>" in out


def test_linkify_escapes_text():
    out = ui.linkify("a <script> & https://x.io/y. end")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert out.endswith("end")


def test_linkify_empty():
    assert ui.linkify("") == ""
    assert ui.linkify(None) == ""


def test_signal_kind_meta_covers_all_kinds():
    for kind in ui.SIGNAL_KINDS:
        meta = ui.signal_kind_meta(kind)
        assert meta["desc"] and meta["icon"] and meta["label"]


def test_sort_headers_toggle_direction():
    f = ui.InboxFilters.from_query(sort="score")
    h = ui.inbox_sort_headers(f)
    score = h[0]
    assert score["active"] and score["dir"] == "desc"
    assert "sort=score_asc" in score["href"]


def test_sort_headers_ascending_link():
    f = ui.InboxFilters.from_query()
    h = ui.inbox_sort_headers(f)
    title = h[1]                      # title column
    assert not title["active"]
    assert "sort=title" in title["href"] and "page=1" in title["href"]


def test_new_sort_keys_query_db(conn):
    rows = db.list_postings(conn, sort="title", status="all", limit=5)
    titles = [r["title"] for r in rows]
    assert titles == sorted(titles, key=str.lower)


def test_newest_oldest_sort(conn):
    newest = db.list_postings(conn, sort="newest", status="all", limit=3)
    oldest = db.list_postings(conn, sort="oldest", status="all", limit=3)
    assert newest[0]["first_seen"] >= newest[-1]["first_seen"]
    assert oldest[0]["first_seen"] <= oldest[-1]["first_seen"]


# ── inbox sortable headers render ────────────────────────────────────────────


def test_inbox_renders_sort_links(client):
    r = client.get("/")
    assert 'class="th-sort' in r.text
    assert 'title="Sort by company"' in r.text


def test_sort_param_filters_through(client):
    r = client.get("/?sort=title")
    assert r.status_code == 200
    assert "th-sort--active" in r.text


# ── profile routes ──────────────────────────────────────────────────────────


def test_profile_page_renders(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Your profile" in r.text
    assert "Full name" in r.text
    assert "Custom fields" in r.text


def test_profile_save_roundtrip(client, monkeypatch):
    import shutil
    import tempfile

    from jobscout.webapp import profile_store
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    r = client.post("/profile/save", data={
        "f_full_name": "Jane Doe",
        "f_email": "jane@example.com",
        "f_current_company": "Acme Trading",
        "f_current_title": "Senior Software Engineer",
        "f_employment_start": "2019-06",
        "f_school": "NUS",
        "f_degree": "BS",
        "cf_touched": "1",
        "cf_label_1": "portfolio",
        "cf_value_1": "https://janedoe.dev",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profile?saved=1"

    text = (tmp / "resume.yaml").read_text()
    assert 'full_name: "Jane Doe"' in text
    assert "Acme Trading" in text
    assert "comments are preserved" or "#" in text  # comments intact
    # comments from the original file survived
    assert "THE source of truth" in text

    values = profile_store.current_values()
    assert values["full_name"] == "Jane Doe"
    assert values["current_company"] == "Acme Trading"

    import shutil as sh
    sh.rmtree(tmp.parent)


def test_profile_save_invalid_rolls_back(client, monkeypatch):
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    before = (tmp / "resume.yaml").read_text()
    # force validation failure by monkeypatching the validator
    import jobscout.core.resume as cr
    orig = cr.load_master_resume

    def broken():
        raise cr.ResumeError("boom")
    monkeypatch.setattr(cr, "load_master_resume", broken)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.core_resume.load_master_resume", broken)

    r = client.post("/profile/save", data={"f_full_name": "X"},
                    follow_redirects=False)
    assert r.status_code == 422
    assert (tmp / "resume.yaml").read_text() == before
    monkeypatch.setattr(cr, "load_master_resume", orig)
    import shutil as sh
    sh.rmtree(tmp.parent)


def core_resume_dir():
    from jobscout.core.paths import master_resume_dir
    return master_resume_dir()


# ── applications apply UI (P9) ──────────────────────────────────────────────

PACKET_SEED = textwrap.dedent("""
INSERT INTO packets (id, posting_id, status, dir)
VALUES ('pk_w1', 'p_int_1', 'packet:ready', '{pkt_dir}');
""")


def _seed_packet(db_file, tmp_path):
    pkt_dir = tmp_path / "pkt"
    pkt_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.executescript(PACKET_SEED.format(pkt_dir=pkt_dir))
    conn.commit()
    conn.close()
    return pkt_dir


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

    from jobscout.webapp import apply as apply_mod
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

    from jobscout.webapp import apply as apply_mod
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
