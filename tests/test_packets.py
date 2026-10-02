"""Tests for the P6 packet pipeline: fill sheet, claim-check gate, tailor
validation, orchestrator end-to-end (fake models, skeleton resume)."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.core import db as core_db
from jobscout.core.schema import (
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

    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert result["packet_id"].startswith("pk-")
    assert result["status"] == "needs_input"
    # skeleton resume → missing fields reported
    assert any("required field" in r for r in result["reasons"])
    # files written
    out = tmp_path / f"jane-street-{today}"
    for name in ("packet.yaml", "fill_sheet.yaml", "claim_check.yaml",
                 "tailor.yaml", "resume.md", "cover_letter.md"):
        assert (out / name).is_file(), name
    # DB state — the packet row carries the CANONICAL status, matching the
    # posting row and the statuses the packet board filters on.
    pk = core_db.get_packet(conn, result["packet_id"])
    assert pk is not None and pk["status"] == "packet:needs_input"
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


def test_render_typst_pdf_end_to_end(tmp_path, monkeypatch):
    """The PDF pipeline: template compiles with typst, data beside it."""
    import shutil

    from jobscout.core.resume import load_master_resume
    from jobscout.packets import render

    if shutil.which("typst") is None:
        import pytest

        pytest.skip("typst not installed")
    resume = load_master_resume()
    pdf = render.render_typst_pdf(tmp_path, None, resume)
    assert pdf is not None and pdf.is_file() and pdf.stat().st_size > 1000
    # the template is copied beside the data (typst path resolution)
    assert (tmp_path / "resume.typ").is_file()
    assert (tmp_path / "packet-data.json").is_file()


# ── portable packets.dir: relative store form + install-path resolution ────


def test_packet_dir_rel_and_resolve_round_trip(tmp_path, monkeypatch):
    from jobscout.core import paths as core_paths

    var = tmp_path / "var"
    (var / "applications" / "acme-2026-09-30").mkdir(parents=True)
    monkeypatch.setattr(core_paths, "var_root", lambda: var)

    out_dir = var / "applications" / "acme-2026-09-30"
    rel = core_paths.packet_dir_rel(out_dir)
    assert rel == "applications/acme-2026-09-30"

    resolved = core_paths.resolve_packet_dir(rel)
    assert resolved == out_dir
    # None / empty → None
    assert core_paths.resolve_packet_dir(None) is None
    assert core_paths.resolve_packet_dir("") is None
    # a dir OUTSIDE var/ stores absolute and resolves as-is
    outside = core_paths.packet_dir_rel(tmp_path / "elsewhere" / "x")
    assert outside.startswith("/")
    assert core_paths.resolve_packet_dir(outside) == tmp_path / "elsewhere" / "x"


def test_resolve_rebases_legacy_absolute_from_foreign_machine(tmp_path,
                                                               monkeypatch):
    """The DB copied from another machine: absolute rows carry that
    machine's path — resolution lands under THIS install's var/."""
    from jobscout.core import paths as core_paths

    var = tmp_path / "var"
    (var / "applications" / "acme-2026-09-30").mkdir(parents=True)
    monkeypatch.setattr(core_paths, "var_root", lambda: var)

    foreign = "/Users/somebodyelse/Personal Project/jobscout/var/applications/acme-2026-09-30"
    resolved = core_paths.resolve_packet_dir(foreign)
    assert resolved == var / "applications" / "acme-2026-09-30"


def test_v9_migration_rewrites_absolute_rows(tmp_path, monkeypatch):
    """init_db on a pre-v9 DB: absolute /var/applications/ rows are
    rewritten to the portable relative form."""
    from jobscout.core.db import schema as core_schema

    db_file = tmp_path / "jobscout.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(core_schema.SCHEMA)
    conn.execute("PRAGMA user_version = 8")
    conn.execute(
        "INSERT INTO packets (id, posting_id, status, dir) VALUES "
        "('p1', 1, 'drafting', "
        "'/Users/old/Personal Project/jobscout/var/applications/acme-x')"
    )
    conn.execute(
        "INSERT INTO packets (id, posting_id, status, dir) VALUES "
        "('p2', 2, 'drafting', 'applications/already-rel')"
    )
    conn.commit()
    conn.close()

    # init_db re-runs migrate on the existing file
    monkeypatch.setattr(core_schema, "db_path", lambda: db_file)
    core_schema.init_db()

    conn = sqlite3.connect(db_file)
    rows = dict(conn.execute("SELECT id, dir FROM packets").fetchall())
    conn.close()
    assert rows["p1"] == "applications/acme-x"        # rebased
    assert rows["p2"] == "applications/already-rel"   # untouched


def test_orchestrator_stores_portable_dir(conn, tmp_path, monkeypatch):
    """prepare() writes the portable store form into packets.dir —
    relative to var/, resolvable from any install path."""
    from jobscout.core import paths as core_paths
    from jobscout.packets.orchestrator import prepare_packet

    # var/ isolated into the test tmp; applications live under it
    monkeypatch.setattr(core_paths, "var_root", lambda: tmp_path)
    monkeypatch.setattr(core_paths, "applications_dir",
                        lambda: tmp_path / "applications")

    result = prepare_packet(conn, "p1", dry_run=True)

    row = conn.execute(
        "SELECT dir FROM packets WHERE id = ?",
        (result["packet_id"],),
    ).fetchone()
    # the stored form is RELATIVE — portable across machines
    assert not row["dir"].startswith("/")
    assert row["dir"].startswith("applications/")
    # and it resolves to the real files under THIS var root
    pd = core_paths.resolve_packet_dir(row["dir"])
    assert pd is not None and pd.is_dir()
    assert (pd / "packet.yaml").is_file()


def test_upsert_packet_stores_canonical_status(conn):
    """upsert_packet must canonicalize the short alias.

    The orchestrator passes "ready"/"needs_input"; the packet board filters on
    "packet:ready"/"packet:needs_input". Storing the alias verbatim made a
    freshly prepared packet render nowhere on /applications.
    """
    for alias, canonical in (("ready", "packet:ready"),
                             ("needs_input", "packet:needs_input"),
                             ("drafting", "packet:drafting")):
        pid = f"pk_{alias}"
        core_db.upsert_packet(conn, packet_id=pid, posting_id="p1", status=alias)
        row = conn.execute("SELECT status FROM packets WHERE id = ?", (pid,)).fetchone()
        assert row["status"] == canonical


def test_upsert_packet_leaves_canonical_status_alone(conn):
    core_db.upsert_packet(conn, packet_id="pk_a", posting_id="p1", status="applied")
    core_db.upsert_packet(conn, packet_id="pk_b", posting_id="p1", status="interviewing")
    core_db.upsert_packet(conn, packet_id="pk_c", posting_id="p1", status="offer")
    for pid in ("pk_a", "pk_b", "pk_c"):
        row = conn.execute("SELECT status FROM packets WHERE id = ?", (pid,)).fetchone()
        assert not row["status"].startswith("packet:")


def test_migrate_canonicalizes_legacy_packet_status(conn):
    """Rows written before the fix keep working: the v12 migration backfills."""
    for i, legacy in enumerate(("ready", "needs_input", "drafting", "applied")):
        conn.execute(
            "INSERT INTO packets (id, posting_id, status) VALUES (?, 'p1', ?)",
            (f"pk_legacy{i}", legacy),
        )
    conn.commit()

    core_db._migrate(conn)
    conn.commit()

    got = {r["id"]: r["status"] for r in conn.execute(
        "SELECT id, status FROM packets WHERE id LIKE 'pk_legacy%'")}
    assert got["pk_legacy0"] == "packet:ready"
    assert got["pk_legacy1"] == "packet:needs_input"
    assert got["pk_legacy2"] == "packet:drafting"
    assert got["pk_legacy3"] == "applied"          # untouched
