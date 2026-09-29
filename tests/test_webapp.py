"""Webapp tests: ui view-model, db filters, and live route behavior.

Route tests run the real FastAPI app against a throwaway file DB via
monkeypatched db.connect — every page renders with seeded data, filters
and pagination behave, and the HTMX partial contract holds.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import db
from jobscout.webapp import ui
from jobscout.webapp.routes import create_app

# ── test data ───────────────────────────────────────────────────────────────

SEED = textwrap.dedent("""
INSERT INTO companies (id, name, domain, tier, non_ats, career_url)
VALUES
  ('acme',  'Acme',    'acme.com',  'A', 0, NULL),
  ('globex', 'Globex', 'globex.com', 'B', 1, 'https://globex.com/careers');

-- 60 new postings for Acme (pagination fodder), varying level/location
WITH RECURSIVE seq(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 60)
INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, remote, rule_pass, status,
                      first_seen, last_seen, content_hash)
SELECT
  'p_new_' || printf('%03d', i),
  CASE WHEN i % 2 = 0 THEN 'ats:greenhouse:acme' ELSE 'careers:jsonld:globex' END,
  CASE WHEN i % 2 = 0 THEN 'acme' ELSE 'globex' END,
  'https://x/' || i, printf('%064d', i),
  'Software Engineer ' || i,
  CASE WHEN i % 3 = 0 THEN 'New York, NY' ELSE 'Singapore' END,
  CASE WHEN i % 4 = 0 THEN 'senior' ELSE NULL END,
  CASE WHEN i % 5 = 0 THEN 1 ELSE 0 END,
  1, 'new', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', printf('%064x', i)
FROM seq;

INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, rule_pass, status, first_seen,
                      last_seen, content_hash)
VALUES
  ('p_int_1', 'ats:greenhouse:acme', 'acme', 'https://x/int1',
   'i1', 'Senior Quant', 'New York, NY', 'senior', 1, 'interested',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'i1x'),
  ('p_dis_1', 'ats:greenhouse:acme', 'acme', 'https://x/dis1',
   'd1', 'Junior Analyst', 'London', 'junior', 1, 'dismissed',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'd1x'),
  ('p_rule_1', 'ats:greenhouse:acme', 'acme', 'https://x/rule1',
   'r1', 'Chef', 'Berlin', NULL, 0, 'new', '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'r1x');
UPDATE companies SET ats_tokens = '{"greenhouse":"acme"}' WHERE id = 'acme';
""")


@pytest.fixture()
def db_file(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    conn.executescript(SEED)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(monkeypatch, db_file):
    def fake_connect():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "init_db", lambda: db_file)
    app = create_app()
    return TestClient(app)


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


def test_all_pages_render(client):
    for path in ("/companies", "/discovery", "/applications", "/ops"):
        assert client.get(path).status_code == 200, path
    r = client.get("/posting/p_int_1")
    assert r.status_code == 200
    assert "Senior Quant" in r.text


def test_companies_shows_dark_pool_and_boards(client):
    text = client.get("/companies").text
    assert "Acme" in text and "Globex" in text
    assert "greenhouse" in text
    assert "dark-pool" in text


def test_unknown_posting_redirects(client):
    r = client.get("/posting/nope", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


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


def test_inbox_renders_sort_links(client):
    r = client.get("/")
    assert 'class="th-sort' in r.text
    assert 'title="Sort by company"' in r.text


def test_sort_param_filters_through(client):
    r = client.get("/?sort=title")
    assert r.status_code == 200
    assert "th-sort--active" in r.text


# ── profile routes ──────────────────────────────────────────────────────────


def test_profile_page_renders(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Your profile" in r.text
    assert "Full name" in r.text
    assert "Custom fields" in r.text


def test_profile_save_roundtrip(client, monkeypatch):
    import shutil
    import tempfile

    from jobscout.webapp import profile_store
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    r = client.post("/profile/save", data={
        "f_full_name": "Jane Doe",
        "f_email": "jane@example.com",
        "f_current_company": "Acme Trading",
        "f_current_title": "Senior Software Engineer",
        "f_employment_start": "2019-06",
        "f_school": "NUS",
        "f_degree": "BS",
        "cf_touched": "1",
        "cf_label_1": "portfolio",
        "cf_value_1": "https://janedoe.dev",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profile?saved=1"

    text = (tmp / "resume.yaml").read_text()
    assert 'full_name: "Jane Doe"' in text
    assert "Acme Trading" in text
    assert "comments are preserved" or "#" in text  # comments intact
    # comments from the original file survived
    assert "THE source of truth" in text

    values = profile_store.current_values()
    assert values["full_name"] == "Jane Doe"
    assert values["current_company"] == "Acme Trading"

    import shutil as sh
    sh.rmtree(tmp.parent)


def test_profile_save_invalid_rolls_back(client, monkeypatch):
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    before = (tmp / "resume.yaml").read_text()
    # force validation failure by monkeypatching the validator
    import jobscout.core.resume as cr
    orig = cr.load_master_resume

    def broken():
        raise cr.ResumeError("boom")
    monkeypatch.setattr(cr, "load_master_resume", broken)
    monkeypatch.setattr(
        "jobscout.webapp.profile_store.core_resume.load_master_resume", broken)

    r = client.post("/profile/save", data={"f_full_name": "X"},
                    follow_redirects=False)
    assert r.status_code == 422
    assert (tmp / "resume.yaml").read_text() == before
    monkeypatch.setattr(cr, "load_master_resume", orig)
    import shutil as sh
    sh.rmtree(tmp.parent)


def core_resume_dir():
    from jobscout.core.paths import master_resume_dir
    return master_resume_dir()


# ── applications apply UI (P9) ──────────────────────────────────────────────

PACKET_SEED = textwrap.dedent("""
INSERT INTO packets (id, posting_id, status, dir)
VALUES ('pk_w1', 'p_int_1', 'packet:ready', '{pkt_dir}');
""")


def _seed_packet(db_file, tmp_path):
    pkt_dir = tmp_path / "pkt"
    pkt_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.executescript(PACKET_SEED.format(pkt_dir=pkt_dir))
    conn.commit()
    conn.close()
    return pkt_dir


def test_applications_page_shows_apply_ui(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.get("/applications")
    assert r.status_code == 200
    assert 'name="pk"' in r.text                       # selection checkboxes
    assert "apply to selected" in r.text
    assert "select all" in r.text
    assert "<th scope=\"col\">details</th>" in r.text  # readiness column
    assert "Apply runs" in r.text                      # live run log
    assert "0 selected" in r.text                      # JS-free default count


def test_applications_details_column_reads_fill_sheet(client, db_file, tmp_path):
    pkt_dir = _seed_packet(db_file, tmp_path)
    (pkt_dir / "fill_sheet.yaml").write_text(
        "fields:\n"
        "- {label: Email, canonical_key: email, required: true,"
        " value: null, confidence: missing}\n")
    r = client.get("/applications")
    assert "1 missing" in r.text
    (pkt_dir / "fill_sheet.yaml").write_text(
        "fields:\n"
        "- {label: Email, canonical_key: email, required: true,"
        " value: x@y, confidence: exact}\n")
    r = client.get("/applications")
    assert "all known" in r.text


def test_applications_apply_launches_and_redirects(client, db_file, monkeypatch):
    _seed_packet(db_file, db_file.parent)
    launched = {}

    def fake_launch(conn, ids):
        launched["ids"] = ids
        db.record_apply_run(conn, packet_id=ids[0], mode="assisted",
                            status="opened", detail="test")
        return {"automated": [], "assisted": ids, "note": []}

    from jobscout.webapp import apply as apply_mod
    monkeypatch.setattr(apply_mod, "launch_apply", fake_launch)
    r = client.post("/applications/apply", data={"pk": "pk_w1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "/applications?applied=1" in r.headers["location"]
    assert launched["ids"] == ["pk_w1"]
    r2 = client.get(r.headers["location"])
    assert "browser open" in r2.text or "Apply runs" in r2.text


def test_applications_apply_error_redirects(client, db_file, monkeypatch):
    _seed_packet(db_file, db_file.parent)

    from jobscout.webapp import apply as apply_mod
    def boom(conn, ids):
        raise apply_mod.ApplyError("nope")

    monkeypatch.setattr(apply_mod, "launch_apply", boom)
    r = client.post("/applications/apply", data={"pk": "pk_w1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=nope" in r.headers["location"]


def test_applications_runs_partial(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    conn = sqlite3.connect(db_file)
    db.record_apply_run(conn, packet_id="pk_w1", mode="automated",
                        status="launched", detail="headed filler run")
    conn.close()
    r = client.get("/applications/runs")
    assert r.status_code == 200
    assert "Apply runs" in r.text
    assert "launched" in r.text
    assert "every 3s" in r.text                        # polls while live


def test_discovery_page_shows_hunting_profile(client):
    r = client.get("/discovery")
    assert r.status_code == 200
    assert "Hunting profile" in r.text
    assert "levels" in r.text and "locations" in r.text


# ── companies page: scrollable panels + row links (UI polish) ───────────────


def test_companies_page_scrollable_and_row_links(client):
    r = client.get("/companies")
    assert r.status_code == 200
    assert r.text.count("scroll-y") >= 2          # table + signal feed
    assert "data-row-link" in r.text              # rows with careers URLs
    assert "text-nowrap" in r.text                # stat titles never wrap


def test_inbox_rows_carry_row_links(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'data-row-link="/posting/' in r.text


def test_applications_rows_carry_row_links(client, db_file, tmp_path):
    _seed_packet(db_file, tmp_path)
    r = client.get("/applications")
    assert 'data-row-link="/packet/pk_w1"' in r.text


# ── button sweep: every referenced URL must resolve to a real route ────────


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


def test_run_agent_now_returns_immediately(client, monkeypatch):
    """The run-agent button: 303 instantly, run happens in background."""
    import subprocess
    import threading
    import time

    done = threading.Event()

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            self.args = args

        def communicate(self, timeout=None):
            done.set()
            time.sleep(0.2)
            return "agent done", ""

        def kill(self):
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    r = client.post("/discovery/run", follow_redirects=False)
    assert r.status_code == 303
    assert "started=1" in r.headers["location"]
    assert done.wait(5)                      # the background thread fired

    status = client.get("/discovery/run-status")
    assert status.status_code == 200
    assert "every 3s" in status.text


# ── live run panel + error surfacing + credentials UX ──────────────────────


def test_run_panel_error_callout(client):
    from jobscout.webapp.routes import _run_state

    _run_state.update(running=False, out="some output",
                      error="ConfigError: missing config file: models.yaml")
    try:
        r = client.get("/discovery/run-status")
        assert r.status_code == 200
        assert "run failed" in r.text
        assert "ConfigError: missing config file" in r.text
    finally:
        _run_state.update(running=False, out="", error="")


def test_run_panel_has_button_and_spend(client):

    r = client.get("/discovery/run-status")
    assert 'action="/discovery/run"' in r.text      # button lives in the panel
    assert "agent spend" in r.text                   # spend refreshes with it


def test_run_panel_running_state_polls(client):
    from jobscout.webapp.routes import _run_state

    _run_state.update(running=True, out="", error="")
    try:
        r = client.get("/discovery/run-status")
        assert "every 3s" in r.text
        assert "full hunt running" in r.text
        assert "Run agent now" not in r.text         # button swapped for state
    finally:
        _run_state.update(running=False, out="", error="")


def test_ops_spend_partial_polls_while_running(client):
    from jobscout.webapp.routes import _run_state

    _run_state.update(running=True, out="", error="")
    try:
        r = client.get("/ops/spend")
        assert r.status_code == 200
        assert "every 3s" in r.text
        assert "live" in r.text
    finally:
        _run_state.update(running=False, out="", error="")


def test_credentials_card_replace_and_remove_semantics(client, db_file):
    r = client.get("/ops")
    assert "Replace the saved key" in r.text
    assert "replaces" in r.text
    # remove button appears only when a key is saved
    assert "remove key" not in r.text
    import pathlib

    from jobscout.webapp import key_store as _ks

    fake = pathlib.Path("/tmp/_fake.env")
    fake.write_text("JOBSCOUT_LLM_API_KEY=sk-zzz\n", encoding="utf-8")
    from jobscout.core import config as core_config

    orig_store, orig_cfg = _ks._env_path, core_config.env_path
    _ks._env_path = lambda: fake
    core_config.env_path = lambda: fake     # status() reads via load_env
    try:
        r = client.get("/ops")
        assert "remove key" in r.text
        assert "data-confirm-prompt" in r.text
        assert "key saved" in r.text and "zzz" in r.text
    finally:
        _ks._env_path, core_config.env_path = orig_store, orig_cfg
        fake.unlink(missing_ok=True)


# ── resume upload / download round-trip ────────────────────────────────────


VALID_RESUME_YAML = """identity:
  full_name: Upload Tester
  email: upload@example.com
experience:
  - id: EXPCUR
    company: Uploaded Co
    title: Engineer
    dates: {start: "2024-01"}
education:
  - id: EDU1
    school: Upload U
    degree: BS
    dates: {start: "2016-09", end: "2020-06"}
"""


def test_resume_upload_replaces_with_backup(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths
    from jobscout.core import resume as cr

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr(cr, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr("jobscout.webapp.profile_store.master_resume_dir",
                        lambda: tmp_path)
    before = (tmp_path / "resume.yaml").read_text(encoding="utf-8")

    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.yaml", VALID_RESUME_YAML,
                                           "application/x-yaml")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "uploaded=1" in r.headers["location"]
    after = (tmp_path / "resume.yaml").read_text(encoding="utf-8")
    assert after == VALID_RESUME_YAML
    assert (tmp_path / "resume.yaml.bak").read_text(encoding="utf-8") == before
    # the form now reflects the uploaded values
    page = client.get("/profile")
    assert "Upload Tester" in page.text and "Uploaded Co" in page.text


def test_resume_upload_invalid_rejected_untouched(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths
    from jobscout.core import resume as cr

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr(cr, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr("jobscout.webapp.profile_store.master_resume_dir",
                        lambda: tmp_path)
    before = (tmp_path / "resume.yaml").read_text(encoding="utf-8")

    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.yaml", "not: [valid",
                                           "application/x-yaml")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "upload_error=" in r.headers["location"]
    assert (tmp_path / "resume.yaml").read_text(encoding="utf-8") == before


def test_resume_upload_rejects_non_yaml(client):
    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.pdf", b"%PDF-",
                                           "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "upload_error=" in r.headers["location"]


def test_resume_download_serves_file(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    r = client.get("/profile/resume-download")
    assert r.status_code == 200
    assert "full_name" in r.text


# ── button failures echo to the user (no silent dead buttons) ──────────────


def test_mode_failure_flashes_on_page(client, monkeypatch):
    from jobscout.core import config as core_config

    def boom(mode):
        raise core_config.ConfigError("yaml exploded")

    monkeypatch.setattr(core_config, "set_discovery_mode", boom)
    # routes import set_discovery_mode inside the handler from core.config
    r = client.post("/discovery/mode", data={"mode": "agent"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "mode change failed" in page.text


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


def test_htmx_failure_toast_handler_present(client):
    r = client.get("/static/js/app.js")
    assert "htmx:responseError" in r.text
    assert "htmx:sendError" in r.text
    assert "request failed" in r.text


# ── external opens + focused hunts ─────────────────────────────────────────


def test_open_url_endpoint_rejects_non_http(client):
    r = client.post("/open-url", data={"url": "file:///etc/passwd"})
    assert r.status_code == 200
    assert r.json()["error"]
    r2 = client.post("/open-url", data={"url": "javascript:alert(1)"})
    assert r2.json()["error"]


def test_open_url_endpoint_opens_default_browser(client, monkeypatch):
    import subprocess

    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = client.post("/open-url", data={"url": "https://acme.com/careers"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [["open", "https://acme.com/careers"]]


def test_run_panel_renders_both_hunt_buttons(client):
    r = client.get("/discovery/run-status")
    assert "full hunt (sweep + agent)" in r.text
    assert "profile hunt only" in r.text
    assert 'name="focus" value="profile"' in r.text
    assert "title=" in r.text                       # tooltips explain them


def test_run_agent_focus_flag_reaches_subprocess(client, monkeypatch):
    import subprocess as sp

    seen = {}

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            seen["args"] = args
            seen["env"] = kw.get("env")

        def communicate(self, timeout=None):
            return "done", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(sp, "Popen", FakePopen)
    r = client.post("/discovery/run", data={"focus": "profile"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen["env"]["JOBSCOUT_AGENT_FOCUS"] == "profile"


def test_run_cancel_kills_and_labels(client, monkeypatch):
    """Cancel mid-run: the process is killed, output labelled cancelled."""
    import subprocess
    import threading
    import time

    from jobscout.webapp.routes import _run_state

    started = threading.Event()

    class FakePopen:
        returncode = None

        def __init__(self, args, **kw):
            self.killed = threading.Event()

        def communicate(self, timeout=None):
            started.set()
            self.killed.wait(5)                 # blocks until kill() interrupts
            return "partial output", ""

        def kill(self):
            self.returncode = -9
            self.killed.set()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = client.post("/discovery/run", follow_redirects=False)
    assert r.status_code == 303
    assert started.wait(5)

    c = client.post("/discovery/run/cancel", follow_redirects=False)
    assert c.status_code == 303
    for _ in range(30):                        # wait for the thread to land
        if not _run_state["running"]:
            break
        time.sleep(0.1)
    assert _run_state["error"] == "run cancelled by you"
