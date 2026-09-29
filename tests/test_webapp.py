"""UI view-model + db filtering + linkify/sorting — the non-route
webapp tests (route tests live in test_routes_*.py, shared fixtures
in conftest.py)."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.core import db
from jobscout.webapp import ui

# ── test data ───────────────────────────────────────────────────────────────


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


# ── profile routes ──────────────────────────────────────────────────────────

