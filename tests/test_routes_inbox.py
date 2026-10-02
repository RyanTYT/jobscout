"""test_routes_inbox — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations

import re

from jobscout.core import db


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



def test_unknown_posting_redirects(client):
    r = client.get("/posting/nope", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"




def test_inbox_renders_sort_links(client):
    r = client.get("/")
    assert 'class="th-sort' in r.text
    assert 'title="Sort by company"' in r.text



def test_sort_param_filters_through(client):
    r = client.get("/?sort=title")
    assert r.status_code == 200
    assert "th-sort--active" in r.text




def test_inbox_rows_carry_row_links(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'data-row-link="/posting/' in r.text



# ── returning from a posting lands back on the page you left ────────────────


def test_posting_links_carry_the_page_you_are_on(client):
    r = client.get("/?page=3&page_size=25")
    assert r.status_code == 200
    links = re.findall(r'data-row-link="([^"]+)"', r.text)
    assert links, "no rows rendered"
    for href in links:
        assert href.startswith("/posting/")
        assert "page=3&amp;page_size=25" in href, href


def test_posting_backlink_returns_to_the_same_page(client):
    r = client.get("/posting/p_int_1?page=3&page_size=25")
    assert r.status_code == 200
    assert 'class="backlink" href="/?page=3&amp;page_size=25"' in r.text


def test_posting_backlink_also_restores_filters(client):
    """Page 3 of an unfiltered list is a different set of postings, so the
    backlink carries the filters too."""
    r = client.get("/posting/p_int_1?page=3&status=interested&sort=title")
    assert r.status_code == 200
    back = re.search(r'class="backlink" href="([^"]+)"', r.text).group(1)
    assert back.startswith("/?")
    for want in ("page=3", "status=interested", "sort=title"):
        assert want in back, back


def test_backlink_defaults_to_page_one_when_reached_without_state(client):
    """Postings opened from Applications, search or a bookmark carry no inbox
    params — that must still render a sane backlink, not a broken one."""
    r = client.get("/posting/p_int_1")
    assert r.status_code == 200
    assert 'class="backlink" href="/?page=1&amp;page_size=50"' in r.text


def test_backlink_rejects_a_hostile_page_param(client):
    """The query string is re-parsed and re-emitted, never reflected — so a
    crafted value cannot escape the href into another origin."""
    r = client.get(
        "/posting/p_int_1?page=3%22%20onmouseover=%22alert(1)"
        "&sort=%3Cscript%3E&evil=//evil.example")
    assert r.status_code == 200
    back = re.search(r'class="backlink" href="([^"]+)"', r.text).group(1)
    assert back == "/?page=1&amp;page_size=50", back
    assert "evil.example" not in r.text
    assert "onmouseover" not in back
    assert "<script>" not in back


def test_backlink_page_is_clamped_to_a_positive_int(client):
    r = client.get("/posting/p_int_1?page=-5")
    assert r.status_code == 200
    assert 'class="backlink" href="/?page=1&amp;page_size=50"' in r.text


def test_backlink_page_size_is_validated_against_page_sizes(client):
    """An arbitrary page_size must not reach the inbox — Pagination clamps it
    to a known size, so the backlink stays a URL the inbox will honour."""
    r = client.get("/posting/p_int_1?page=2&page_size=9999")
    assert r.status_code == 200
    back = re.search(r'class="backlink" href="([^"]+)"', r.text).group(1)
    assert back == "/?page=2&amp;page_size=50", back


def test_status_buttons_post_the_page_context(client):
    """The swapped-in row keeps the user's place: the buttons carry the inbox
    query string, and the response re-renders the row with it intact."""
    page = client.get("/?page=2&page_size=25&status=new")
    pid = re.findall(r'data-row-link="/posting/([^?&"]+)', page.text)[0]

    r = client.post(
        f"/posting/{pid}/status?page=2&page_size=25",
        data={"status": "interested"},
        headers={"HX-Request": "true", "HX-Target": f"#row-{pid}"},
    )
    assert r.status_code == 200
    assert f'data-row-link="/posting/{pid}?page=2&amp;page_size=25"' in r.text


def test_inbox_status_buttons_include_the_page(client):
    page = client.get("/?page=2&page_size=25")
    posts = re.findall(r'hx-post="(/posting/[^"]+/status\?[^"]*)"', page.text)
    assert posts, "status buttons lost their page context"
    assert all("page=2&amp;page_size=25" in p for p in posts)



def test_prepare_failure_flashes_on_posting(client, monkeypatch):
    from jobscout.packets import orchestrator as orch

    def boom(conn, pid, dry_run, force):
        raise orch.PacketError("resume exploded")

    monkeypatch.setattr(orch, "prepare_packet", boom)
    # the handler imports prepare_packet from the orchestrator module
    import jobscout.webapp.routes as routes_mod  # noqa: F401

    r = client.post("/posting/p_int_1/prepare", data={"dry_run": "true"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "packet preparation failed" in page.text



# ── interested → the Applications board ──────────────────────────────────────
# The board lists packets, so "Mark Interested" has to leave a packet row
# behind or the entry never shows up there.


def test_interested_creates_a_packet_row(client, conn):
    client.post("/posting/p_new_001/status", data={"status": "interested"},
                headers={"HX-Request": "true", "HX-Target": "#row-p_new_001"})
    pk = conn.execute(
        "SELECT * FROM packets WHERE posting_id = 'p_new_001'").fetchone()
    assert pk is not None
    assert pk["status"] == "packet:drafting"
    # deterministic id, matching what prepare_packet will upsert onto
    assert pk["id"] == f"pk-{db.sha256('p_new_001')[:12]}"
    # no artefacts yet — the packet is a placeholder until it is prepared
    assert pk["dir"] is None


def test_interested_entry_appears_on_the_applications_board(client):
    client.post("/posting/p_new_002/status", data={"status": "interested"},
                headers={"HX-Request": "true", "HX-Target": "#row-p_new_002"})
    r = client.get("/applications")
    assert r.status_code == 200
    assert "Software Engineer 2" in r.text
    # the board says why there is no fill sheet yet
    assert "no fill sheet yet" in r.text


def test_interested_toast_mentions_applications(client):
    r = client.post("/posting/p_new_003/status", data={"status": "interested"},
                    headers={"HX-Request": "true", "HX-Target": "#row-p_new_003"})
    from urllib.parse import unquote

    assert "added to Applications" in unquote(r.headers["X-Toast"])


def test_dismissed_does_not_create_a_packet(client, conn):
    client.post("/posting/p_new_004/status", data={"status": "dismissed"},
                headers={"HX-Request": "true", "HX-Target": "#row-p_new_004"})
    n = conn.execute(
        "SELECT COUNT(*) FROM packets WHERE posting_id = 'p_new_004'"
    ).fetchone()[0]
    assert n == 0


def test_re_marking_interested_does_not_reset_an_applied_packet(client, conn):
    """upsert_packet's ON CONFLICT overwrites status — the guard matters."""
    conn.execute(
        "INSERT INTO packets (id, posting_id, status, dir, applied_at) "
        "VALUES ('pk_live', 'p_new_005', 'applied', 'applications/x', "
        "'2026-09-28')")
    conn.commit()
    client.post("/posting/p_new_005/status", data={"status": "interested"},
                headers={"HX-Request": "true", "HX-Target": "#row-p_new_005"})
    pk = conn.execute(
        "SELECT * FROM packets WHERE posting_id = 'p_new_005'").fetchone()
    assert pk["status"] == "applied", "an in-flight application was reset"
    assert pk["applied_at"] == "2026-09-28"
    # and no second packet row was created alongside it
    n = conn.execute(
        "SELECT COUNT(*) FROM packets WHERE posting_id = 'p_new_005'"
    ).fetchone()[0]
    assert n == 1


def test_interested_is_idempotent(client, conn):
    for _ in range(3):
        client.post("/posting/p_new_006/status", data={"status": "interested"},
                    headers={"HX-Request": "true", "HX-Target": "#row-p_new_006"})
    n = conn.execute(
        "SELECT COUNT(*) FROM packets WHERE posting_id = 'p_new_006'"
    ).fetchone()[0]
    assert n == 1
