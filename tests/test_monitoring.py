"""Tests for monitoring + new discovery sources (P8 dark-pool extension)."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.core import db as core_db
from jobscout.sources.discovery import monitoring

RICH_CAREERS_HTML = """
<html><body>
<h1>Careers at TestCo</h1>
<ul>
<li>Senior Software Engineer</li>
<li>Quant Developer</li>
<li>DevOps Engineer</li>
</ul>
</body></html>
"""

CHANGED_CAREERS_HTML = """
<html><body>
<h1>Careers at TestCo</h1>
<ul>
<li>Senior Software Engineer</li>
<li>Quant Developer</li>
<li>DevOps Engineer</li>
<li>Staff FPGA Engineer</li>
<li>Machine Learning Scientist</li>
</ul>
</body></html>
"""

SITEMAP_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://testco.com/</loc></url>
  <url><loc>https://testco.com/about</loc></url>
  <url><loc>https://testco.com/jobs/senior-engineer</loc></url>
  <url><loc>https://testco.com/jobs/quant-dev</loc></url>
</urlset>
"""

SITEMAP_XML_V2 = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://testco.com/</loc></url>
  <url><loc>https://testco.com/about</loc></url>
  <url><loc>https://testco.com/jobs/senior-engineer</loc></url>
  <url><loc>https://testco.com/jobs/quant-dev</loc></url>
  <url><loc>https://testco.com/jobs/fpga-engineer</loc></url>
  <url><loc>https://testco.com/jobs/ml-scientist</loc></url>
</urlset>
"""


def test_extract_job_titles():
    titles = monitoring.extract_job_titles(RICH_CAREERS_HTML)
    assert "Senior Software Engineer" in titles
    assert "Quant Developer" in titles
    assert "DevOps Engineer" in titles
    assert len(titles) == 3


def test_extract_job_titles_empty():
    assert monitoring.extract_job_titles("") == []
    assert monitoring.extract_job_titles("<html><body>no jobs here</body></html>") == []


def test_title_diff():
    old = monitoring.extract_job_titles(RICH_CAREERS_HTML)
    new = monitoring.extract_job_titles(CHANGED_CAREERS_HTML)
    added = [t for t in new if t not in set(old)]
    removed = [t for t in old if t not in set(new)]
    assert "Staff FPGA Engineer" in added
    assert "Machine Learning Scientist" in added
    assert len(added) == 2
    assert removed == []


def test_sitemap_job_urls():
    urls = monitoring._parse_locs(SITEMAP_XML)
    job_urls = [u for u in urls if "/jobs/" in u]
    assert len(job_urls) == 2
    assert "https://testco.com/jobs/senior-engineer" in job_urls


def test_sitemap_diff():
    old_urls = set(monitoring._parse_locs(SITEMAP_XML))
    new_urls = set(monitoring._parse_locs(SITEMAP_XML_V2))
    new = [u for u in new_urls if u not in old_urls]
    assert len(new) == 2
    assert "https://testco.com/jobs/fpga-engineer" in new


def test_github_org_guessing():
    from jobscout.sources.discovery.github import _guess_orgs
    guesses = _guess_orgs("wintermute.com")
    assert "wintermute" in guesses
    assert "wintermute-io" in guesses
    assert _guess_orgs("") == []


def test_news_hiring_re():
    from jobscout.sources.discovery.news import _HIRING_RE
    assert _HIRING_RE.search("Company raises $50M to expand team")
    assert _HIRING_RE.search("New engineering roles open")
    assert not _HIRING_RE.search("Company announces new product")


def test_hn_hiring_context_re():
    from jobscout.sources.discovery.hn import _HIRING_CONTEXT_RE
    assert _HIRING_CONTEXT_RE.search("we're growing the team")
    assert _HIRING_CONTEXT_RE.search("join our team")
    assert not _HIRING_CONTEXT_RE.search("nice weather today")


def test_news_rss_parsing():
    from jobscout.sources.discovery.news import _parse_rss
    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>TestCo raises $50M</title>
    <link>https://example.com/article1</link>
    <pubDate>Mon, 28 Sep 2026</pubDate>
  </item>
  <item><title>no link</title></item>
</channel></rss>"""
    entries = _parse_rss(rss)
    assert len(entries) == 1
    assert entries[0]["title"] == "TestCo raises $50M"
    assert _parse_rss("<not-xml") == []


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    c.execute(
        "INSERT INTO companies (id, name, domain, tier, non_ats, career_url) "
        "VALUES ('testco', 'TestCo', 'testco.com', 'A', 1, 'https://testco.com/careers')"
    )
    c.commit()
    yield c
    c.close()


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200


class FakeClient:
    """Returns pre-canned responses for specific URL patterns."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def get(self, url, params=None):
        for pattern, text in self.responses.items():
            if pattern in url:
                return FakeResponse(text)
        return FakeResponse("")


def test_page_change_first_run_baseline(conn):
    """First run should store hash but emit no signal."""
    client = FakeClient({"careers": RICH_CAREERS_HTML})
    stats = monitoring.check_page_changes(client, conn)
    assert stats["companies_checked"] == 1
    assert stats["changes_detected"] == 0
    assert stats["signals_new"] == 0
    assert core_db.get_state(conn, "page_hash:testco") is not None


def test_page_change_detected(conn):
    """Second run with changed content should emit a signal with the title diff."""
    client1 = FakeClient({"careers": RICH_CAREERS_HTML})
    monitoring.check_page_changes(client1, conn)  # baseline

    client2 = FakeClient({"careers": CHANGED_CAREERS_HTML})
    stats = monitoring.check_page_changes(client2, conn)
    assert stats["changes_detected"] == 1
    assert stats["signals_new"] == 1

    signals = conn.execute(
        "SELECT * FROM signals WHERE kind = 'careers_page_changed'"
    ).fetchall()
    assert len(signals) == 1
    assert "FPGA Engineer" in signals[0]["note"]
    assert "+2" in signals[0]["note"]


def test_sitemap_change_detected(conn):
    """Sitemap diff should emit signals for new job URLs."""
    client1 = FakeClient({"sitemap.xml": SITEMAP_XML})
    monitoring.check_sitemap_changes(client1, conn)  # baseline

    client2 = FakeClient({"sitemap.xml": SITEMAP_XML_V2})
    stats = monitoring.check_sitemap_changes(client2, conn)
    assert stats["new_urls"] >= 2  # 2 new job URLs (may be duplicated across paths)
    assert stats["signals_new"] >= 2

    signals = conn.execute(
        "SELECT * FROM signals WHERE kind = 'sitemap_new_url'"
    ).fetchall()
    urls = {s["note"].split(": ")[-1] for s in signals}
    assert "https://testco.com/jobs/fpga-engineer" in urls
