"""webapp/reports.py — reading run reports off disk for the report browser.

The report browser's templates were written against a structured report API
that no route ever provided. This module supplies it from two sources:

  * the markdown the runs already write (var/digest/*.md for the daily
    sweep, var/morning_reports/*.md for the agent) — read as `raw`
  * the database, for the parts a human acts on: which companies appeared
    that day, which signals landed, which candidates are still unpromoted

Nothing here writes. Promotion is the watchlist's job.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobscout.core import paths as core_paths

KINDS = ("daily", "morning")


class ReportError(Exception):
    """The report could not be read (bad kind/date, or missing on disk)."""


def _dir_for(kind: str) -> Path:
    if kind == "daily":
        return core_paths.digest_dir()
    if kind == "morning":
        return core_paths.morning_reports_dir()
    raise ReportError(f"unknown report kind {kind!r} — expected one of {KINDS}")


def _dates_for(kind: str) -> list[str]:
    d = _dir_for(kind)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.md"):
        stem = p.stem
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            out.append(stem)
    return sorted(out, reverse=True)


def _date_matches(row_value: str | None, date: str) -> bool:
    return bool(row_value) and str(row_value)[:10] == date


def list_reports(conn: sqlite3.Connection) -> dict:
    """Sidebar context: the date lists, newest first, with an unpromoted flag
    on morning entries so an agent find is not lost."""
    daily = [{"date": d} for d in _dates_for("daily")]
    morning = []
    for d in _dates_for("morning"):
        rows = _companies_added(conn, d)
        morning.append({
            "date": d,
            "has_unpromoted": any(r["tier"] == "candidate" for r in rows),
        })
    return {"daily_reports": daily, "morning_reports": morning}


def report_path(kind: str, date: str) -> Path:
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise ReportError(f"bad report date {date!r}")
    p = _dir_for(kind) / f"{date}.md"
    if not p.is_file():
        raise ReportError(f"no {kind} report for {date}")
    return p


def load_report(conn: sqlite3.Connection, kind: str, date: str) -> dict:
    """The viewer context for one report."""
    if kind not in KINDS:
        raise ReportError(f"unknown report kind {kind!r}")
    raw = report_path(kind, date).read_text(encoding="utf-8")
    added = _companies_added(conn, date)
    return {
        "kind": kind,
        "date": date,
        # the promotion table: agents report what they found, the sweep
        # reports the candidates it surfaced
        "companies_added": added if kind == "morning" else None,
        "candidates": added if kind == "daily" else None,
        "signals_added": _signals_added(conn, date),
        "db_changes": _db_changes(raw),
        "raw": raw,
    }


def _companies_added(conn: sqlite3.Connection, date: str) -> list[dict]:
    """Companies first written on this date, in the shape the promotion table
    wants. `suggested_tier` is the agent's own recommendation — surfaced so the
    owner can accept it; it never moves a company by itself."""
    # Companies with NO tier at all come first — they are the ones that still
    # need a decision, and they would otherwise sink below alphabetically
    # earlier rows. `tier` is nullable (the add-URL seeder writes NULL), so
    # this is a real state, not a hypothetical. SQLite sorts 0 before 1,
    # hence DESC on the boolean.
    rows = conn.execute(
        "SELECT id, name, domain, tier, notes, suggested_tier FROM companies "
        "WHERE substr(created_at, 1, 10) = ? "
        "ORDER BY (tier IS NULL OR tier = '') DESC, name", (date,)
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "domain": r["domain"],
             "tier": r["tier"], "note": r["notes"],
             "suggested_tier": r["suggested_tier"]} for r in rows]


def _signals_added(conn: sqlite3.Connection, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT s.kind, s.note, s.company_id, c.name AS company_name "
        "FROM signals s LEFT JOIN companies c ON s.company_id = c.id "
        "WHERE substr(s.seen_at, 1, 10) = ? ORDER BY s.seen_at", (date,)
    ).fetchall()
    return [{"kind": r["kind"], "note": r["note"],
             "company_name": r["company_name"] or r["company_id"] or "?"}
            for r in rows]


def _db_changes(raw: str) -> list[str]:
    """The report's own auditable diff, read back out of its markdown.

    Stops at the next heading, at a `---` rule, or at the trailing `state:`
    summary line — none of which are changes.
    """
    out: list[str] = []
    in_section = False
    for line in raw.splitlines():
        stripped = line.strip()
        if line.startswith("## "):
            in_section = line[3:].strip().lower().startswith("db changes")
            continue
        if not in_section or stripped in ("", "---"):
            continue
        if stripped.startswith("state:"):
            break
        out.append(stripped.lstrip("- ").strip())
    return [o for o in out if o and o != "none"]
