"""Tests for the careers-page crawler (P3): JSON-LD, board links, feeds, sitemap."""

from __future__ import annotations

from jobscout.sources.careers_page import (
    extract_jobpostings,
    jsonld_to_posting,
    parse_wp_feed,
    scan_board_links,
)

JOB = {
    "@type": "JobPosting",
    "title": "Senior Quant Developer",
    "description": "<p>Rust + market data</p>",
    "datePosted": "2026-09-20",
    "employmentType": "FULL_TIME",
    "jobLocation": {
        "address": {"addressLocality": "New York", "addressRegion": "NY"}
    },
    "url": "https://example.com/careers/quant-1",
}


def _html(jsonld: str) -> str:
    return f"<html><body><script type=\"application/ld+json\">{jsonld}</script></body></html>"


def test_extract_single_object():
    import json

    html = _html(json.dumps(JOB))
    assert extract_jobpostings(html) == [JOB]


def test_extract_graph_and_array():
    import json

    graph = {"@graph": [JOB, {"@type": "Organization", "name": "x"}]}
    assert extract_jobpostings(_html(json.dumps(graph))) == [JOB]
    arr = [JOB, JOB]
    assert len(extract_jobpostings(_html(json.dumps(arr)))) == 2


def test_extract_ignores_bad_json():
    assert extract_jobpostings(_html("{not json")) == []
    assert extract_jobpostings("<html>no scripts</html>") == []


def test_jsonld_to_posting_mapping():
    p = jsonld_to_posting(JOB, "https://example.com/careers", "Example Co")
    assert p is not None
    assert p.title == "Senior Quant Developer"
    assert p.location == "New York, NY"
    assert "Rust + market data" in (p.description or "")
    assert p.source == "careers:example.com"
    assert p.company_slug == "example-co"
    assert jsonld_to_posting({"@type": "JobPosting", "title": ""}, "https://x", "y") is None


def test_scan_board_links():
    html = (
        '<a href="https://job-boards.greenhouse.io/acme/jobs/1">apply</a>'
        '<a href="https://boards.greenhouse.io/embed/job_board?for=acme2">board</a>'
        '<a href="https://jobs.lever.co/acme-llc/abc123">job</a>'
        "<a href='https://jobs.ashbyhq.com/acme.3'>x</a>"
    )
    tokens = scan_board_links(html)
    assert tokens["greenhouse"] == "acme"
    assert tokens["lever"] == "acme-llc"  # first path segment, not posting id
    assert tokens["ashby"] == "acme.3"


def test_scan_board_links_empty():
    assert scan_board_links("<html><a href='/careers'>careers</a></html>") == {}


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>C++ Trading Engineer</title>
    <link>https://example.com/jobs/cpp-1/</link>
    <description><![CDATA[Low latency everything.]]></description>
    <pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate>
  </item>
  <item><title>no link</title></item>
</channel></rss>
"""


def test_parse_wp_feed():
    items = parse_wp_feed(RSS, "Example Co", "https://example.com/jobs/feed/")
    assert len(items) == 1  # the link-less item is dropped
    assert items[0].title == "C++ Trading Engineer"
    assert items[0].url == "https://example.com/jobs/cpp-1/"
    assert items[0].source == "careers:example.com"


def test_parse_wp_feed_bad_xml():
    assert parse_wp_feed("<not-xml", "x", "https://x/feed/") == []
