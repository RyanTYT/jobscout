"""Tests for P4 discovery: RSS parsing, HN comments, CSE rotation, signals."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.core import db as core_db
from jobscout.core.schema import ProfileCfg
from jobscout.sources.discovery.blocklist import is_blocked
from jobscout.sources.discovery.cse import queries_for_today
from jobscout.sources.discovery.hn import keyword_match, parse_comment
from jobscout.sources.discovery.rss import _entries, _funding_name

PROFILE = ProfileCfg(
    profile_version="t",
    target=__import__("jobscout.core.schema", fromlist=["TargetCfg"]).TargetCfg(
        roles=["quant developer"], stack=["rust", "c++"], domains=["market-data"],
    ),
)

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Acme raises $30M Series A to build market data rails</title>
    <link>https://example.com/acme-raises</link>
    <pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate>
    <description>Acme announced a round.</description>
  </item>
  <item>
    <title>Another headline without funding words</title>
    <link>https://example.com/other</link>
  </item>
</channel></rss>
"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom Co nets $12M</title>
    <link href="https://example.com/atom"/>
    <updated>2026-09-22T00:00:00Z</updated>
    <summary>Round.</summary>
  </entry>
</feed>
"""


def test_entries_rss_and_atom():
    items = _entries(RSS_XML)
    assert len(items) == 2
    assert items[0]["title"].startswith("Acme raises")
    assert items[0]["link"] == "https://example.com/acme-raises"
    atom = _entries(ATOM_XML)
    assert len(atom) == 1
    assert atom[0]["link"] == "https://example.com/atom"
    assert _entries("<not-xml") == []


def test_funding_name():
    assert _funding_name("Acme raises $30M Series A") == "Acme"
    assert _funding_name("Fintech startup Ledgr secures $8M seed") == "Ledgr"
    assert _funding_name("Nothing to see here") is None
    assert _funding_name("A very extremely long company name with more words than allowed raises $1M") is None


def test_parse_comment_pipe_format():
    company, domains = parse_comment(
        "Acme Corp | New York | Onsite | Rust/Quant devs — https://acme.com/careers"
    )
    assert company == "Acme Corp"
    assert domains == ["acme.com"]


def test_parse_comment_colon_and_url_guard():
    company, _ = parse_comment("https://acme.com/jobs | NYC | engineers")
    assert company is None  # URL-first lines are not company names
    company, _ = parse_comment("Ledgr: hiring backend engineers")
    assert company == "Ledgr"


def test_parse_comment_domain_blocklist():
    _, domains = parse_comment("Foo | remote | see https://github.com/foo/jobs and https://foo.io")
    assert domains == ["foo.io"]


def test_parse_comment_email_and_bare_domain():
    company, domains = parse_comment(
        "Acme | NYC | Rust devs — apply jobs@acme.com or see acme.io/careers"
    )
    assert company == "Acme"
    assert domains == ["acme.com", "acme.io"]
    # personal emails are not company domains
    _, domains = parse_comment("Solo dev | remote | hit me at cooldev@gmail.com")
    assert domains == []
    # bare mention without scheme counts
    _, domains = parse_comment("Ledgr | Berlin | https://ledgr.de and www.ledgr.io")
    assert domains == ["ledgr.de", "ledgr.io"]


def test_keyword_match():
    assert keyword_match("We need a rust engineer", PROFILE)
    assert keyword_match("quant developer with python", PROFILE)
    assert not keyword_match("We need a pastry chef", PROFILE)


def test_is_blocked():
    assert is_blocked("www.linkedin.com")
    assert is_blocked("news.ycombinator.com")
    assert is_blocked("jobs.lever.co")
    assert not is_blocked("acme.io")


def test_cse_rotation_deterministic():
    import datetime as dt

    d = dt.date(2026, 9, 28)
    a = queries_for_today(PROFILE, 5, d)
    b = queries_for_today(PROFILE, 5, d)
    assert a == b and len(a) == 5
    nxt = queries_for_today(PROFILE, 5, d + dt.timedelta(days=1))
    assert nxt != a  # rotation advances by day
    assert len(set(queries_for_today(PROFILE, 30, d))) >= 10  # bank is big enough


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    yield c
    c.close()


def test_upsert_signal_dedup(conn):
    assert db_upsert(conn) is True
    assert db_upsert(conn) is False  # same kind+key → dedup
    rows = conn.execute("SELECT * FROM signals").fetchall()
    assert len(rows) == 1
    assert rows[0]["company_id"] == "wintermute"


def db_upsert(conn) -> bool:
    return core_db.upsert_signal(
        conn, kind="funding", key="https://example.com/wm",
        company_id="wintermute", note="Wintermute raises $50M",
    )


def test_state_kv(conn):
    assert core_db.get_state(conn, "hn_last_story") is None
    core_db.set_state(conn, "hn_last_story", "42")
    assert core_db.get_state(conn, "hn_last_story") == "42"
    core_db.set_state(conn, "hn_last_story", "43")
    assert core_db.get_state(conn, "hn_last_story") == "43"
