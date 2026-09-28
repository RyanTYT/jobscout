"""Tests for the P6 packet pipeline: fill sheet, claim-check gate, tailor
validation, orchestrator end-to-end (fake models, skeleton resume)."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.core import db as core_db
from jobscout.core.models import (
    Bullet,
    Dates,
    Experience,
    Identity,
    MasterResume,
)
from jobscout.packets import claim_check as cc
from jobscout.packets import field_map as fm
from jobscout.packets import tailor as tl

RICH_RESUME = MasterResume(
    version=3,
    identity=Identity(full_name="Ryan Tan", email="ryan@example.com"),
    experience=[
        Experience(
            id="EXP1",
            company="Binance",
            title="Senior Software Engineer",
            dates=Dates(start="2022-01", end="present"),
            bullets=[
                Bullet(id="EXP1-B1", text="Built matching engine",
                       metrics=["38% latency reduction"]),
                Bullet(id="EXP1-B2", text="Led market data team"),
            ],
        )
    ],
)


def test_fill_sheet_skeleton_reports_missing():
    sheet = fm.build_fill_sheet(MasterResume())
    missing = fm.sheet_missing(sheet)
    labels = {e.label for e in missing}
    assert "Full Name" in labels and "Email" in labels
    salary = next(e for e in sheet if e.canonical_key == "salary_expectation")
    assert salary.confidence == "policy"


def test_fill_sheet_rich_resume():
    sheet = fm.build_fill_sheet(RICH_RESUME)
    full_name = next(e for e in sheet if e.canonical_key == "full_name")
    assert full_name.value == "Ryan Tan" and full_name.confidence == "exact"
    current = next(e for e in sheet if e.canonical_key == "current_company")
    assert current.value == "Binance"


def test_claim_check_fabrication_detected():
    rows = cc.check_text(
        "Reduced latency by 47% across the matching engine.",
        RICH_RESUME, None,
    )
    unsupported = [r for r in rows if r["status"] == "unsupported"]
    assert any("47%" in r["claim"] for r in unsupported)
    assert not cc.gate(rows)  # gate blocks ready


def test_claim_check_true_metric_passes():
    rows = cc.check_text(
        "Delivered a 38% latency reduction on the matching engine.",
        RICH_RESUME, None,
    )
    assert not [r for r in rows if r["status"] == "unsupported"]
    assert cc.gate(rows)


def test_claim_check_entity_from_posting_ok():
    posting = {"title": "Quant Developer", "description": "Jane Street role",
               "company_name": "Jane Street"}
    rows = cc.check_text("Excited about Jane Street.", RICH_RESUME, posting)
    entity_rows = [r for r in rows if r["kind"] == "entity"]
    assert entity_rows and all(r["status"] == "verified" for r in entity_rows)


def test_tailor_plan_strips_unknown_ids():
    plan, issues = tl.parse_plan(
        '{"experience_order": ["EXP1", "GHOST"], '
        '"bullets": {"EXP1": ["EXP1-B1", "FAKE-B9"], "NOPE": ["EXP1-B2"]}, '
        '"rephrased": [{"source_id": "EXP1-B2", "text": "x", "change": "y"}, '
        '{"source_id": "FAKE-B9", "text": "y", "change": "z"}]}',
        RICH_RESUME,
    )
    assert "GHOST" not in plan["experience_order"]
    assert plan["bullets"] == {"EXP1": ["EXP1-B1"]}  # FAKE-B9 + NOPE dropped
    assert [r["source_id"] for r in plan["rephrased"]] == ["EXP1-B2"]
    assert len(issues) >= 3  # GHOST, FAKE-B9, NOPE, FAKE rephrase


def test_tailor_plan_bad_json():
    plan, issues = tl.parse_plan("not json", RICH_RESUME)
    assert plan == {} and issues


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    c.execute(
        "INSERT INTO companies (id, name, domain, tier, ats_tokens) "
        "VALUES ('janestreet', 'Jane Street', 'janestreet.com', 'A', '{}')"
    )
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title, "
        "location, rule_pass, first_seen, last_seen, status) "
        "VALUES ('p1', 'ats:greenhouse:janestreet', 'janestreet', "
        "'https://example.com/1', 'h1', 'ASIC Engineer', 'NYC', 1, "
        "'2026-09-28', '2026-09-28', 'interested')"
    )
    c.commit()
    yield c
    c.close()


def test_orchestrator_end_to_end_skeleton(conn, tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths
    from jobscout.packets.orchestrator import prepare_packet

    monkeypatch.setattr(core_paths, "applications_dir", lambda: tmp_path)
    result = prepare_packet(conn, "p1", dry_run=True)

    assert result["packet_id"].startswith("pk-")
    assert result["status"] == "needs_input"
    # skeleton resume → missing fields reported
    assert any("required field" in r for r in result["reasons"])
    # files written
    out = tmp_path / "jane-street-2026-09-28"
    for name in ("packet.yaml", "fill_sheet.yaml", "claim_check.yaml",
                 "tailor.yaml", "resume.md", "cover_letter.md"):
        assert (out / name).is_file(), name
    # DB state
    pk = core_db.get_packet(conn, result["packet_id"])
    assert pk is not None and pk["status"] == "needs_input"
    posting = core_db.get_posting(conn, "p1")
    assert posting["status"] == "packet:needs_input"
    # claim check on dry-run (fact-free) content passes
    assert result["unsupported"] == 0


def test_mark_applied_updates_packet(conn, tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths
    from jobscout.packets.orchestrator import prepare_packet

    monkeypatch.setattr(core_paths, "applications_dir", lambda: tmp_path)
    result = prepare_packet(conn, "p1", dry_run=True)
    assert core_db.set_posting_status(conn, "p1", "applied")
    core_db.get_packet_for_posting(conn, "p1")
    assert core_db.set_packet_status(conn, result["packet_id"], "applied")
    assert core_db.get_packet(conn, result["packet_id"])["status"] == "applied"
