"""test_routes_companies — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations


def test_companies_shows_dark_pool_and_boards(client):
    text = client.get("/companies").text
    assert "Acme" in text and "Globex" in text
    assert "greenhouse" in text
    assert "dark-pool" in text





def test_companies_page_scrollable_and_row_links(client):
    r = client.get("/companies")
    assert r.status_code == 200
    assert r.text.count("scroll-y") >= 2          # table + signal feed
    assert "data-row-link" in r.text              # rows with careers URLs
    assert "text-nowrap" in r.text                # stat titles never wrap





# ── application routing: apply / email / monitor ──────────────────────────


def test_company_route_decision():
    from jobscout.webapp import ui

    apply_row = {"rule_pass_total": 3, "contact_email": None}
    email_row = {"rule_pass_total": 0, "contact_email": "jobs@dark.co"}
    monitor_row = {"rule_pass_total": 0, "contact_email": None}
    assert ui.company_route(apply_row)["kind"] == "apply"
    assert ui.company_route(email_row)["kind"] == "email"
    assert ui.company_route(monitor_row)["kind"] == "monitor"
    counts = ui.route_counts([apply_row, apply_row, email_row, monitor_row])
    assert counts == {"apply": 2, "email": 1, "monitor": 1}


def test_extract_contact_email_prefers_hiring_intent():
    from jobscout.sources.postings.careers import extract_contact_email

    html = ('<a href="mailto:careers@acme.io">join</a> noreply@acme.io '
            'support@acme.io someone@gmail.com privacy@acme.io')
    assert extract_contact_email(html) == "careers@acme.io"
    assert extract_contact_email("webmaster@x.com abuse@y.org") is None
    assert extract_contact_email("logo@x.co.png x@x.com") == "x@x.com"
    assert extract_contact_email("") is None


def test_add_company_records_contact_email():
    import sqlite3

    from jobscout.core import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)

    class Ctx:
        pass

    ctx = Ctx()
    ctx.conn = conn
    ctx.wl = None
    ctx.client = None
    # add_candidate needs a wl; call the db write directly via the tool's path
    db.upsert_company(conn, name="Dark Pool Co", domain="darkpool.co",
                      contact_email="jobs@darkpool.co")
    row = conn.execute("SELECT contact_email FROM companies "
                       "WHERE id = 'dark-pool-co'").fetchone()
    assert row["contact_email"] == "jobs@darkpool.co"


def test_companies_page_renders_route_column(client):
    r = client.get("/companies")
    assert "route" in r.text
    assert "apply route" in r.text and "email route" in r.text
    # acme has rule-pass postings in the seed → an apply badge
    assert "text-bg-success" in r.text


def test_companies_survive_a_row_with_no_domain_and_no_careers_url(client, conn):
    """An agent find with nothing resolved yet must not take the page down.

    `'https://' + None` raised TypeError inside the row loop, so ONE such row
    500'd the whole list (133 of 238 real rows).
    """
    conn.execute(
        "INSERT INTO companies (id, name, domain, career_url, tier) "
        "VALUES ('nodomain', 'No Domain Co', NULL, NULL, 'candidate')")
    conn.commit()
    r = client.get("/companies")
    assert r.status_code == 200
    assert "No Domain Co" in r.text
    # no row-link attributes were invented for it
    assert "https://None" not in r.text


def test_companies_row_link_still_present_when_resolvable(client):
    r = client.get("/companies")
    assert r.status_code == 200
    assert 'data-row-link="https://acme.com"' in r.text
