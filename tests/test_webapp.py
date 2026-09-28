"""Webapp tests: ui view-model, db filters, and live route behavior.

Route tests run the real FastAPI app against a throwaway file DB via
monkeypatched db.connect — every page renders with seeded data, filters
and pagination behave, and the HTMX partial contract holds.
"""

from __future__ import annotations

import sqlite3
import textwrap

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
