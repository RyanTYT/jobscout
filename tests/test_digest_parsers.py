"""Tests for the markdown report readers in ops/digest.py.

These three functions were uncommitted on 2026-10-01 and destroyed by a
`git filter-repo --force` reset; they were restored from the surviving
CPython 3.13 bytecode. These tests pin the restored behaviour so a future
rewrite cannot quietly drift from the version that was lost.

Deliberately separate from tests/test_reports.py: that module covers the
webapp browser (database-backed, companies.notes), this one covers the
markdown readers (watchlist-coverage ATS note). They are not substitutes.
"""

from __future__ import annotations

import os
import time

import pytest

from jobscout.ops import digest


@pytest.fixture()
def dirs(monkeypatch, tmp_path):
    """Point the report dirs at a tmp tree and return them."""
    daily = tmp_path / "digest"
    morning = tmp_path / "morning"
    daily.mkdir()
    morning.mkdir()
    monkeypatch.setattr("jobscout.core.paths.digest_dir", lambda: daily)
    monkeypatch.setattr("jobscout.core.paths.morning_reports_dir", lambda: morning)
    return daily, morning


DAILY = """# jobscout digest — 2026-09-30

**2 new postings passed rules** (156 excluded by rules).

## New today — rule pass

| # | title | company | location | level | source |
|---|-------|---------|----------|-------|--------|
| 1 | [Field Executive Architect](https://boards.example/1) | anthropic | SF, CA | senior | greenhouse/anthropic |
| 2 | [AI Engineer](https://boards.example/2) | flow | Amsterdam | | greenhouse/flow |

## Discovery

- candidate: DiaMonTech AG
- signal: Ramp: federal contract probe

## Watchlist coverage

| company | tier | ATS boards | jobs | new | note |
|---------|------|------------|------|-----|------|
| Jane Street | A | greenhouse:janestreet | 230 | 0 | OCaml shop, flat culture |
| Hudson River | A | none found (dark-pool entry) | 0 | 0 | no public ATS board |

## Source errors (1)

- job-sites: FOREIGN KEY constraint failed

---

run 16 · 37 companies · mode: pipeline
"""

MORNING = """# jobscout morning report — 2026-09-28

## Final summary

Found three quant shops.

## DB changes (auditable diff)

- +company DiaMonTech AG
- +company IOMED

## Tool log

### step 1 · `add_company`
- args: `{"name": "DiaMonTech AG", "domain": "diamontech.de"}`
- result: added

### step 2 · `add_signal`
- args: `{"company": "Ramp", "kind": "funding", "note": "raised $25M"}`
- result: added

---

state: companies=38 (was 36)
"""


def _write(directory, date: str, body: str):
    p = directory / f"{date}.md"
    p.write_text(body, encoding="utf-8")
    return p


# ── list_reports ────────────────────────────────────────────────────────────


def test_list_reports_newest_first(dirs):
    daily, _ = dirs
    _write(daily, "2026-09-28", DAILY)
    _write(daily, "2026-09-30", DAILY)
    os.utime(daily / "2026-09-28.md", (time.time() - 100, time.time() - 100))
    out = digest.list_reports("daily")
    assert [r["date"] for r in out] == ["2026-09-30", "2026-09-28"]
    assert all(r["kind"] == "daily" for r in out)
    assert all({"date", "path", "size", "mtime", "kind"} == set(r) for r in out)


def test_list_reports_retention_reaps_old_files(dirs):
    """Reading the list deletes reports past the retention window."""
    daily, _ = dirs
    old = _write(daily, "2020-01-01", DAILY)
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    assert digest.list_reports("daily", retention_days=30) == []
    assert not old.exists(), "stale report should have been unlinked"


def test_list_reports_missing_dir_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("jobscout.core.paths.digest_dir", lambda: tmp_path / "nope")
    assert digest.list_reports("daily") == []


def test_list_reports_morning_kind(dirs):
    _, morning = dirs
    _write(morning, "2026-09-28", MORNING)
    out = digest.list_reports("morning")
    assert [r["date"] for r in out] == ["2026-09-28"]


# ── parse_daily_report ──────────────────────────────────────────────────────


def test_parse_daily_new_postings(dirs):
    daily, _ = dirs
    p = _write(daily, "2026-09-30", DAILY)
    d = digest.parse_daily_report(p)
    assert d["kind"] == "daily" and d["date"] == "2026-09-30"
    assert len(d["new_postings"]) == 2
    first = d["new_postings"][0]
    assert first["title"] == "Field Executive Architect"
    assert first["url"] == "https://boards.example/1"
    assert first["company"] == "anthropic"
    assert first["seniority"] == "senior"
    # a row with an empty level cell still parses
    assert d["new_postings"][1]["seniority"] == ""


def test_parse_daily_skips_header_and_separator(dirs):
    daily, _ = dirs
    p = _write(daily, "2026-09-30", DAILY)
    titles = [r["title"] for r in digest.parse_daily_report(p)["new_postings"]]
    assert "#" not in titles and not any(set(t) == {"-"} for t in titles)


def test_parse_daily_coverage_note_is_the_ats_note(dirs):
    """The coverage note is the board note from write_daily — NOT companies.notes."""
    daily, _ = dirs
    p = _write(daily, "2026-09-30", DAILY)
    companies = digest.parse_daily_report(p)["companies"]
    by_name = {c["name"]: c for c in companies}
    assert by_name["Jane Street"]["note"] == "OCaml shop, flat culture"
    assert by_name["Jane Street"]["tier"] == "A"
    assert by_name["Jane Street"]["ats"] == "greenhouse:janestreet"
    assert by_name["Hudson River"]["ats"] == "none found (dark-pool entry)"


def test_parse_daily_candidates_and_errors(dirs):
    daily, _ = dirs
    p = _write(daily, "2026-09-30", DAILY)
    d = digest.parse_daily_report(p)
    assert d["candidates"] == [{"name": "DiaMonTech AG", "found_via": "discovery"}]
    assert d["errors"] == ["job-sites: FOREIGN KEY constraint failed"]


def test_parse_daily_raw_is_the_whole_file(dirs):
    daily, _ = dirs
    p = _write(daily, "2026-09-30", DAILY)
    assert digest.parse_daily_report(p)["raw"] == DAILY


def test_parse_daily_missing_file_degrades(dirs):
    daily, _ = dirs
    d = digest.parse_daily_report(daily / "1999-01-01.md")
    assert d["date"] == "1999-01-01" and d["kind"] == "daily"
    assert d["new_postings"] == [] and d["raw"] == ""


# ── parse_morning_report ────────────────────────────────────────────────────


def test_parse_morning_tool_log(dirs):
    _, morning = dirs
    p = _write(morning, "2026-09-28", MORNING)
    d = digest.parse_morning_report(p)
    assert d["kind"] == "morning" and d["date"] == "2026-09-28"
    assert d["companies_added"] == [
        {"name": "DiaMonTech AG", "domain": "diamontech.de"}]
    assert d["signals_added"] == [
        {"company": "Ramp", "kind": "funding", "note": "raised $25M"}]


def test_parse_morning_db_changes_and_summary(dirs):
    _, morning = dirs
    p = _write(morning, "2026-09-28", MORNING)
    d = digest.parse_morning_report(p)
    assert d["db_changes"] == ["+company DiaMonTech AG", "+company IOMED"]
    assert "Found three quant shops." in d["final_summary"]


def test_parse_morning_empty_db_changes_is_empty_list(dirs):
    """harness writes a bare "## DB changes" + "- none" when nothing changed."""
    _, morning = dirs
    p = _write(morning, "2026-09-28", MORNING.replace(
        "## DB changes (auditable diff)\n\n- +company DiaMonTech AG\n- +company IOMED",
        "## DB changes\n\n- none"))
    d = digest.parse_morning_report(p)
    assert d["db_changes"] == []


def test_parse_morning_drops_internal_tool_key(dirs):
    _, morning = dirs
    p = _write(morning, "2026-09-28", MORNING)
    assert "_current_tool" not in digest.parse_morning_report(p)


def test_parse_morning_bad_json_is_skipped(dirs):
    _, morning = dirs
    p = _write(morning, "2026-09-28", MORNING.replace(
        '{"name": "DiaMonTech AG", "domain": "diamontech.de"}', "{not json}"))
    d = digest.parse_morning_report(p)
    assert d["companies_added"] == []
    # parsing continues past the bad line
    assert d["signals_added"] == [
        {"company": "Ramp", "kind": "funding", "note": "raised $25M"}]


def test_parse_morning_missing_file_degrades(dirs):
    _, morning = dirs
    d = digest.parse_morning_report(morning / "1999-01-01.md")
    assert d["date"] == "1999-01-01" and d["kind"] == "morning"
    assert d["companies_added"] == [] and d["raw"] == ""


# ── round trip: write_daily output must parse back ──────────────────────────


def test_write_daily_output_parses_back(monkeypatch, tmp_path):
    """The reader and the writer agree on the format."""
    # ops.digest does `from jobscout.core.paths import digest_dir`, binding the
    # name at import time — patching core.paths alone would write to the REAL
    # var/digest. Patch the consumer.
    monkeypatch.setattr(digest, "digest_dir", lambda: tmp_path)
    if True:
        coverage = [{"name": "Jane Street", "tier": "A",
                     "ats": {"greenhouse": "janestreet"}, "jobs": 230, "new": 0,
                     "note": "OCaml shop; dark-pool entry"}]
        rows = [{"title": "AI Engineer", "url": "https://x/1",
                 "company_id": "acme", "location": "Singapore",
                 "seniority": "senior", "source": "ats:greenhouse"}]
        p = digest.write_daily(
            "2026-09-30", coverage=coverage, new=rows, excluded_count=3,
            errors=["boom"], closing=[], run_meta={"run_id": 1, "companies": 1,
                                                  "postings_seen": 9},
        )
        d = digest.parse_daily_report(p)
        assert d["new_postings"][0]["title"] == "AI Engineer"
        assert d["new_postings"][0]["url"] == "https://x/1"
        assert d["companies"][0]["note"] == "OCaml shop; dark-pool entry"
        assert d["companies"][0]["ats"] == "greenhouse:janestreet"
        assert d["errors"] == ["boom"]
