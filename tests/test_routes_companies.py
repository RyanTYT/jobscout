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



