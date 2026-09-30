"""Email tracking: the IMAP client (fake imaplib), the tracker's
matching/classification/monotonic state rules, draft-first replies, the
stores, and the routes. No network, no LLM, no real mailbox."""

from __future__ import annotations

import email as email_lib
import email.utils
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import db
from jobscout.webapp.runners import email_tracker as et

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
    monkeypatch.setattr(core_paths, "master_resume_dir",
                        lambda: REPO / "master_resume")
    monkeypatch.setattr(core_paths, "config_dir", lambda: d)
    return d


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    f = tmp_path / ".env"
    f.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_paths, "env_path", lambda: f)
    return f


@pytest.fixture()
def conn(cfg_dir):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db.upsert_company(c, name="Acme", domain="acme.io", tier="A")
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title,"
        " rule_pass, status, first_seen, last_seen, content_hash)"
        " VALUES ('p1', 'ats:x:acme', 'acme', 'https://x/1', 'h1', 'Eng',"
        " 1, 'new', '2026-09-29', '2026-09-29', 'c1')")
    c.execute(
        "INSERT INTO packets (id, posting_id, status, dir)"
        " VALUES ('pk1', 'p1', 'applied', '/tmp/x')")
    c.commit()
    yield c
    c.close()


# ── mail client (fake imaplib) ─────────────────────────────────────────────


def _raw_email(from_addr, subject, body, msgid="<m1@x>"):
    msg = email_lib.message_from_string(textwrap.dedent(f"""
        From: {from_addr}
        To: you@gmail.com
        Subject: {subject}
        Message-ID: {msgid}
        Date: {email.utils.formatdate()}

        {body}
    """).lstrip())
    return msg.as_bytes()


class FakeImap:
    def __init__(self, messages):     # {uid: raw_bytes}
        self.messages = messages
        self.appended = []

    def login(self, u, p):
        self.user = u
        return ("OK", [b"Logged in"])

    def select(self, box, readonly=False):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "search":
            uids = sorted(self.messages)
            return ("OK", [b" ".join(str(u).encode() for u in uids)])
        if command == "fetch":
            uid = int(args[0])
            return ("OK", [(b"1 (RFC822 {n})", self.messages[uid])])
        raise AssertionError(f"unexpected uid command {command}")

    def list(self):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"',
                       b'(\\HasNoChildren) "/" "[Gmail]/Drafts"'])

    def append(self, mailbox, flags, date, raw):
        self.appended.append((mailbox, raw))
        return ("OK", [b"Append completed"])

    def logout(self):
        return ("BYE", [b"Bye"])


def test_mailclient_fetch_and_parse(monkeypatch):
    from jobscout.clients import mail as mailmod

    msgs = {5: _raw_email("hr@acme.io", "Interview",
                          "We would like to schedule an interview."),
            9: _raw_email("no-reply@gh.greenhouse.io", "Application received",
                          "Thanks for applying to Acme.")}
    fake = FakeImap(msgs)
    monkeypatch.setattr(mailmod.imaplib, "IMAP4_SSL",
                        lambda host, port: fake)
    client = mailmod.MailClient("you@gmail.com", "apppass")
    mails = client.fetch_new(0)
    assert [m["uid"] for m in mails] == [5, 9]
    assert mails[0]["from_domain"] == "acme.io"
    assert mails[0]["subject"] == "Interview"
    assert "schedule an interview" in mails[0]["body"]
    assert mails[0]["message_id"]


def test_mailclient_append_draft_first(monkeypatch):
    from jobscout.clients import mail as mailmod

    fake = FakeImap({})
    monkeypatch.setattr(mailmod.imaplib, "IMAP4_SSL",
                        lambda host, port: fake)
    client = mailmod.MailClient("you@gmail.com", "apppass")
    mailbox = client.append_draft("hr@acme.io", "Re: Interview",
                                  "Thank you — happy to talk.",
                                  in_reply_to="<m1@x>", from_name="You")
    assert mailbox == "[Gmail]/Drafts"
    assert len(fake.appended) == 1
    raw = fake.appended[0][1]
    parsed = email_lib.message_from_bytes(raw)
    assert parsed["To"] == "hr@acme.io"
    assert parsed["In-Reply-To"] == "<m1@x>"
    assert "happy to talk" in parsed.get_payload()
    # the client has NO send capability at all — append only


# ── matching + monotonic state rules ──────────────────────────────────────


def _mail(from_addr, subject, body=""):
    return {"uid": 1, "from_addr": from_addr,
            "from_domain": from_addr.rpartition("@")[2],
            "subject": subject, "sent_at": None, "message_id": "<x>",
            "body": body}


def test_match_by_domain_and_ats_name():
    companies = [{"id": "acme", "name": "Acme", "domain": "acme.io",
                  "packet_ids": [("pk1", "applied")], "status": "applied"}]
    assert et._match(_mail("hr@acme.io", "hi"), companies)["id"] == "acme"
    assert et._match(_mail("x@mail.acme.io", "hi"), companies)["id"] == "acme"
    assert et._match(
        _mail("no-reply@gh.greenhouse.io", "Your Acme application"),
        companies)["id"] == "acme"                     # ATS + name
    assert et._match(_mail("spam@other.io", "Acme"), companies) is None


def test_advance_monotonic_rules(conn):
    companies = [{"id": "acme", "name": "Acme", "domain": "acme.io",
                  "packet_ids": [("pk1", "applied")]}]
    pid, st = et._advance(conn, companies[0], "interview_invite")
    assert (pid, st) == ("pk1", "interviewing")
    # forward-only: invite again does nothing
    companies[0]["packet_ids"] = [("pk1", "interviewing")]
    assert et._advance(conn, companies[0], "interview_invite")[1] is None
    # offer from interviewing works
    assert et._advance(conn, companies[0], "offer")[1] == "offer"
    # rejection never auto-demotes an offer
    companies[0]["packet_ids"] = [("pk1", "offer")]
    assert et._advance(conn, companies[0], "rejection")[1] is None
    # rejection from interviewing lands
    companies[0]["packet_ids"] = [("pk1", "interviewing")]
    assert et._advance(conn, companies[0], "rejection")[1] == "rejected"
    assert conn.execute("SELECT status FROM packets WHERE id='pk1'"
                        ).fetchone()[0] == "rejected"


# ── check_once end-to-end with fakes ─────────────────────────────────────


class FakeLlm:
    def __init__(self, classification, draft=None):
        self.cls = classification
        self.draft = draft
        self.calls = []

    def chat(self, tier, messages, json_mode=True, cache_key=None, **kw):
        self.calls.append(tier)

        class R:
            text = json.dumps(
                {"class": self.cls, "confidence": "high"}
                if tier == "bulk" else
                (self.draft or {"subject": "Re: Interview", "body": "Thanks!"}))
            model = "fake"
            cost_usd = 0.001
        return R()


def test_check_once_full_flow(monkeypatch, cfg_dir, env_file, tmp_path):
    # check_once opens its OWN connection — give it a shared file DB
    db_file = tmp_path / "flow.db"
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db.upsert_company(c, name="Acme", domain="acme.io", tier="A")
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title,"
        " rule_pass, status, first_seen, last_seen, content_hash)"
        " VALUES ('p1', 'ats:x:acme', 'acme', 'https://x/1', 'h1', 'Eng',"
        " 1, 'new', '2026-09-29', '2026-09-29', 'c1')")
    c.execute(
        "INSERT INTO packets (id, posting_id, status, dir)"
        " VALUES ('pk1', 'p1', 'applied', '/tmp/x')")
    c.commit()
    c.close()

    def fake_connect():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        return cc

    monkeypatch.setattr(db, "connect", fake_connect)
    conn = fake_connect()
    env_file.write_text(
        "JOBSCOUT_EMAIL_USER=you@gmail.com\nJOBSCOUT_EMAIL_PASS=pw\n")
    from jobscout.clients import mail as mailmod

    msgs = {12: _raw_email("hr@acme.io", "Interview invitation",
                           "We'd like to schedule a call.")}
    fake_imap = FakeImap(msgs)
    monkeypatch.setattr(mailmod.imaplib, "IMAP4_SSL",
                        lambda host, port: fake_imap)
    monkeypatch.setattr(et, "_classify",
                        lambda m: ("interview_invite", "high"))
    monkeypatch.setattr(et, "_draft_reply",
                        lambda c, m, co, u: {"subject": "Re: Interview invitation",
                                             "body": "Thank you."})
    summary = et.check_once()
    assert summary == {"checked": 1, "matched": 1}
    # state moved
    assert conn.execute("SELECT status FROM packets WHERE id='pk1'"
                        ).fetchone()[0] == "interviewing"
    # event recorded with the draft detail
    row = db.recent_email_events(conn)[0]
    assert row["classification"] == "interview_invite"
    assert "state: interviewing" in row["action"]
    detail = json.loads(row["detail"])
    assert detail["draft_subject"] == "Re: Interview invitation"
    # the DRAFT was appended to the mailbox — never sent
    assert len(fake_imap.appended) == 1
    # uid checkpoint stored
    assert db.get_state(conn, "email_last_uid") == "12"


def test_check_once_unconfigured_raises(conn):
    from jobscout.clients.mail import MailError

    with pytest.raises(MailError):
        et.check_once()


# ── stores ────────────────────────────────────────────────────────────────


def test_save_email_settings_roundtrip(cfg_dir):
    from jobscout.webapp.stores import settings_store as ss

    ss.save_email(enabled=True, poll_minutes="10")
    cfg = ss.current_email()
    assert cfg.enabled and cfg.poll_minutes == 10
    with pytest.raises(ss.SettingsStoreError):
        ss.save_email(enabled=True, poll_minutes="0")


def test_email_account_store(env_file):
    from jobscout.webapp.stores import key_store as ks

    st = ks.email_status()
    assert not st["configured"]
    ks.save_email_account(user="you@gmail.com", password="apppass",
                          host="")
    text = env_file.read_text(encoding="utf-8")
    assert "JOBSCOUT_EMAIL_USER=you@gmail.com" in text
    assert "JOBSCOUT_EMAIL_PASS=apppass" in text
    assert ks.email_status()["configured"]
    # empty password keeps the saved one
    ks.save_email_account(user="you@gmail.com", password="", host="x")
    assert "apppass" in env_file.read_text(encoding="utf-8")
    with pytest.raises(ks.KeyStoreError):
        ks.save_email_account(user="not-an-email", password="", host="")
    ks.clear_email_account()
    assert not ks.email_status()["configured"]


# ── routes ────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, cfg_dir, env_file, tmp_path):
    db_file = tmp_path / "route.db"
    c = sqlite3.connect(db_file)
    c.executescript(db.SCHEMA)
    db._migrate(c)
    c.execute("INSERT INTO companies (id, name, domain, tier)"
              " VALUES ('jane-street', 'Jane Street', 'janestreet.com', 'A')")
    c.execute("INSERT INTO postings (id, source, company_id, url, url_hash,"
              " title, rule_pass, status, first_seen, last_seen,"
              " content_hash) VALUES ('p1', 's', 'jane-street', 'u', 'h',"
              " 'T', 1, 'new', 'd', 'd', 'ch')")
    c.execute("INSERT INTO packets (id, posting_id, status, dir)"
              " VALUES ('pk_w1', 'p1', 'applied', '/tmp/x')")
    c.commit()
    c.close()

    def fake_connect():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        return cc

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(et, "check_now", lambda: None)   # no background work
    monkeypatch.setattr(et, "maybe_start", lambda: False)
    return TestClient(_create())


def _create():
    from jobscout.webapp.routes import create_app

    return create_app()


def test_ops_email_card_and_save(client, cfg_dir, env_file):
    r = client.get("/ops")
    assert 'name="email_user"' in r.text and "Email tracking" in r.text
    assert "never sends" in r.text
    r2 = client.post("/ops/email-account", data={
        "email_user": "you@gmail.com", "email_pass": "apppass",
        "email_host": "", "poll_minutes": "20", "enabled": "1",
    }, follow_redirects=False)
    assert r2.status_code == 303
    assert "email=1" in r2.headers["location"]
    from jobscout.webapp.stores import settings_store as ss

    assert ss.current_email().enabled
    assert "you@gmail.com" in env_file.read_text(encoding="utf-8")
    page = client.get("/ops")
    assert "tracking on · every 20 min" in page.text


def test_applications_email_check_and_panel(client, cfg_dir):
    # disabled → flash error
    r = client.post("/applications/email-check", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    # enabled → fires + the panel renders events
    from jobscout.webapp.stores import settings_store as ss

    ss.save_email(enabled=True, poll_minutes="15")
    import sqlite3

    c = sqlite3.connect(cfg_dir.parent / "route.db")
    db.record_email_event(
        c, company_id="jane-street", packet_id="pk_w1",
        from_addr="hr@janestreet.com", subject="Interview",
        sent_at=None, message_id="<i>", classification="interview_invite",
        action="state: interviewing; draft saved",
        detail=json.dumps({"draft_subject": "Re: Interview",
                           "draft_body": "Thanks"}))
    c.commit()
    c.close()
    r2 = client.post("/applications/email-check", follow_redirects=False)
    assert r2.status_code == 303
    page = client.get("/applications")
    assert "Email activity" in page.text
    assert "interview invite" in page.text
    assert "in your mail Drafts" in page.text


def test_follow_up_generation_full_flow(monkeypatch, tmp_path,
                                         cfg_dir, env_file):
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).parent))

    """The nudge: packet context → draft → (no email linked) → stored."""
    import sqlite3

    from jobscout.webapp.runners import outreach as orr

    db_file = tmp_path / "fu.db"
    c = sqlite3.connect(db_file)
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db.upsert_company(c, name="Acme", domain="acme.io", tier="A")
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title,"
        " rule_pass, status, first_seen, last_seen, content_hash)"
        " VALUES ('p1', 's', 'acme', 'u', 'h', 'Engineer', 1, 'new',"
        " 'd', 'd', 'ch')")
    c.execute(
        "INSERT INTO packets (id, posting_id, status, applied_at)"
        " VALUES ('pk1', 'p1', 'applied', '2026-09-01')")
    c.commit()
    c.close()

    def fake_connect():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        return cc

    monkeypatch.setattr(db, "connect", fake_connect)

    class FakeLlm:
        def chat(self, tier, messages, json_mode=True, **kw):

            class R:
                text = '{"subject": "Following up on my application", '\
                       '"body": "Gracious nudge."}'
                model = "fake"
                cost_usd = 0.001
            return R()

    monkeypatch.setattr(orr, "_llm", lambda: FakeLlm())
    orr._generate_follow_up(fake_connect(), "pk1")
    row = db.latest_outreach(fake_connect(), "acme", "follow_up")
    assert row["status"] == "done"
    draft = json.loads(row["content"])
    assert draft["subject"].startswith("Following up")
    assert draft["detail"]["days"] >= 0
