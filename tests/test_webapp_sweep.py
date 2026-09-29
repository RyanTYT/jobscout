"""test_webapp_sweep — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations

from tests.conftest import _seed_packet


def test_all_pages_render(client):
    for path in ("/companies", "/discovery", "/applications", "/ops"):
        assert client.get(path).status_code == 200, path
    r = client.get("/posting/p_int_1")
    assert r.status_code == 200
    assert "Senior Quant" in r.text





def test_every_button_url_matches_a_route(client, db_file, tmp_path):
    """Catches URL mismatches (button posts somewhere no route listens —
    the run-agent 404/500 class of bug) across every page, statically:
    hrefs, form actions, and HTMX hx-get/hx-post/hx-boost targets."""
    import re as _re

    from jobscout.webapp.routes import create_app as _create

    _seed_packet(db_file, tmp_path)
    app = _create()
    registered = set()
    prefixes = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path:
            base = (path.split("{")[0].rstrip("/") or "/")
            registered.add(base)
            if "{" in path and base != "/":
                prefixes.add(base)

    url_re = _re.compile(
        r'(?:href|action|hx-get|hx-post|hx-boost)="(/[^"]*)"')
    pages = ["/", "/companies", "/discovery", "/applications", "/ops",
             "/profile", "/posting/p_int_1", "/packet/pk_w1",
             "/applications/runs", "/discovery/run-status"]
    checked = 0
    for page in pages:
        r = client.get(page)
        assert r.status_code == 200, f"{page} -> {r.status_code}"
        for url in url_re.findall(r.text):
            base = url.split("?")[0].split("#")[0].rstrip("/") or "/"
            if base.startswith("/static"):
                continue
            ok = (base in registered
                  or any(base.startswith(pre + "/") for pre in prefixes))
            assert ok, (
                f"{page} references {url!r} but no route is registered "
                f"for {base!r}")
            checked += 1
    assert checked >= 40, f"sweep too small ({checked}) — page render broken?"



