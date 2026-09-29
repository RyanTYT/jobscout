"""Outreach generation: cold-email + linkedin drafting, search parsing,
routes, and the runner lifecycle. LLM + search are faked — no network."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import db
from jobscout.webapp.runners import outreach as orr

CONFIG_FILES = ("models.yaml", "settings.yaml", "watchlist.yaml",
                "profile.yaml")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    d = tmp_path / "config"
    d.mkdir()
    for name in CONFIG_FILES:
        (d / name).write_text((REPO / "config" / name).read_text(encoding="utf-8"))
    # resume: the repo's master resume
    monkeypatch.setattr(core_paths, "master_resume_dir",
                        lambda: REPO / "master_resume")
    monkeypatch.setattr(core_paths, "config_dir", lambda: d)
    return d


@pytest.fixture()
def conn(cfg_dir):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db.upsert_company(c, name="Dark Pool Co", domain="darkpool.co",
                      contact_email="jobs@darkpool.co", tier="dark")
    db.upsert_company(c, name="Matching Co", domain="matching.co", tier="A")
    # matching.co has a rule-passed posting → apply route
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title,"
        " rule_pass, status, first_seen, last_seen, content_hash)"
        " VALUES ('p1', 'ats:x:matching', 'matching-co', 'https://x/1', 'h1',"
        " 'Engineer', 1, 'new', '2026-09-29', '2026-09-29', 'c1')")
    c.commit()
    yield c
    c.close()


# ── search parsing ──────────────────────────────────────────────────────────


def test_parse_results_format():
    text = ("1. Jane Recruiter — https://www.linkedin.com/in/jane-rec\n"
            "   Talent acquisition at Dark Pool Co\n"
            "2. Bob the Builder — https://www.linkedin.com/in/bob\n")
    hits = orr._parse_results(text)
    assert hits[0]["url"] == "https://www.linkedin.com/in/jane-rec"
    assert "Talent acquisition" in hits[0]["snippet"]
    assert hits[1]["name" if False else "title"] == "Bob the Builder"


def test_find_linkedin_contacts_filters_and_dedupes(monkeypatch):
    class Ctx:
        settings = None

    results = [
        '1. A — https://www.linkedin.com/in/a\n   x',
        '2. B — https://www.linkedin.com/in/b\n   y',
        '3. B again — https://www.linkedin.com/in/b\n   dup',
        '4. not-li — https://example.com/nope\n   no',
    ]
    monkeypatch.setattr(orr, "_search", lambda q, n, ctx: "\n".join(results))
    contacts = orr.find_linkedin_contacts("Dark Pool Co", Ctx())
    assert [c["url"] for c in contacts] == [
        "https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]


# ── generation with a fake LLM ─────────────────────────────────────────────


class FakeLlm:
    def __init__(self):
        self.calls = []

    def chat(self, tier, messages, json_mode=True, **kw):
        self.calls.append((tier, messages))

        class R:
            text = json.dumps({
                "subject": "Interest in Dark Pool Co",
                "body": "plain honest email body",
                "contacts": [{"name": "Jane Recruiter",
                              "url": "https://www.linkedin.com/in/jane",
                              "connection_note": "short note",
                              "message": "longer message"}],
            })
            model = "fake-model"
            cost_usd = 0.001
        return R()


def test_generate_cold_email(conn, monkeypatch):
    fake = FakeLlm()
    monkeypatch.setattr(orr, "_llm", lambda: fake)
    orr._generate(conn, "dark-pool-co", "cold_email")
    row = db.latest_outreach(conn, "dark-pool-co", "cold_email")
    assert row["status"] == "done"
    draft = json.loads(row["content"])
    assert draft["subject"].startswith("Interest in")
    assert "never invent" in fake.calls[0][1][0]["content"]  # system honesty


def test_generate_linkedin_searches_then_drafts(conn, monkeypatch):
    fake = FakeLlm()
    monkeypatch.setattr(orr, "_llm", lambda: fake)
    monkeypatch.setattr(orr, "_search",
                        lambda q, n, ctx: "1. Jane — https://www.linkedin.com/in/jane\n   TA")
    orr._generate(conn, "dark-pool-co", "linkedin")
    row = db.latest_outreach(conn, "dark-pool-co", "linkedin")
    assert row["status"] == "done"
    draft = json.loads(row["content"])
    assert draft["contacts"][0]["url"].endswith("/in/jane")


def test_generate_linkedin_no_contacts_fails_cleanly(conn, monkeypatch):
    monkeypatch.setattr(orr, "_search", lambda q, n, ctx: "no results")
    monkeypatch.setattr(orr, "_llm", lambda: FakeLlm())
    orr._generate(conn, "dark-pool-co", "linkedin")
    row = db.latest_outreach(conn, "dark-pool-co", "linkedin")
    assert row["status"] == "failed"
    assert "no LinkedIn profiles" in row["error"]


# ── routes ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, cfg_dir, tmp_path):
    db_file = tmp_path / "route.db"
    c = sqlite3.connect(db_file)
    c.executescript(db.SCHEMA)
    db._migrate(c)
    c.execute("INSERT INTO companies (id, name, domain, tier) VALUES "
              "('jane-street', 'Jane Street', 'janestreet.com', 'A')")
    c.commit()
    c.close()

    def fake_connect():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        return cc

    monkeypatch.setattr(db, "connect", fake_connect)
    # no background thread in route tests: generation runs inline on demand
    monkeypatch.setattr(orr, "start", lambda cid, kind: None)
    return TestClient(_create())


def _create():
    from jobscout.webapp.routes import create_app

    return create_app()


def test_outreach_partial_renders_states(client, tmp_path):
    c = sqlite3.connect(tmp_path / "route.db")
    db.upsert_outreach(c, company_id="jane-street", kind="cold_email",
                       status="done",
                       content=json.dumps({"subject": "hi", "body": "yo"}))
    db.upsert_outreach(c, company_id="jane-street", kind="linkedin",
                       status="failed", error="no contacts")
    c.close()
    r = client.get("/companies/jane-street/outreach")
    assert r.status_code == 200
    assert "Subject:" in r.text and "copy body" in r.text
    assert "no contacts" in r.text                       # failed callout
    assert "regenerate" in r.text


def test_outreach_start_route(client):
    r = client.post("/companies/jane-street/outreach/cold_email",
                    follow_redirects=False)
    assert r.status_code == 303
    assert "/companies/jane-street" in r.headers["location"]


def test_company_detail_renders_outreach_card(client):
    r = client.get("/companies/jane-street")
    assert r.status_code == 200
    assert "Outreach" in r.text
    assert "generate cold email" in r.text
    assert "linkedin reachout" in r.text


def test_companies_page_badges_for_zero_posting_companies(client, monkeypatch):
    # a company with no postings → monitor route → both badges
    from jobscout.webapp import ui

    rows = [{"id": "zeta", "rule_pass_total": 0, "contact_email": None,
             "career_url": None, "domain": "zeta.co"}]
    assert ui.company_route(rows[0])["kind"] == "monitor"
    # template-level: badges render for non-apply routes
    r = client.get("/companies")
    assert "outreach=cold_email" in r.text or "cold email" in r.text
