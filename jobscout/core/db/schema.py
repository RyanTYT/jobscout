"""core/db/schema.py — DDL, migrations, lifecycle.

The database layer is a package: schema.py owns what the database IS
(tables, versions, connect/init/status); queries.py owns what you DO
with it (every CRUD function). core.db re-exports both, so
`from jobscout.core import db` and `db.anything` work unchanged.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jobscout.core import paths as core_paths


def db_path() -> Path:
    return core_paths.db_path()


SCHEMA_VERSION = 9

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          TEXT PRIMARY KEY,          -- slug, e.g. 'janestreet'
    name        TEXT NOT NULL,
    domain      TEXT,
    tier        TEXT CHECK (tier IN ('A','B','C','candidate','dark')),
    ats_tokens  TEXT,                      -- JSON: {greenhouse: token, lever: token, ...}
    career_url  TEXT,
    contact_email TEXT,                   -- careers/contact inbox (crawl or agent)
    non_ats     INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS postings (
    id            TEXT PRIMARY KEY,        -- 'p_<date>_<hash8>'
    source        TEXT NOT NULL,           -- 'ats:greenhouse:drw'
    company_id    TEXT,
    url           TEXT NOT NULL,
    url_hash      TEXT NOT NULL UNIQUE,    -- sha256(url)
    title         TEXT,
    location      TEXT,
    remote        INTEGER,
    seniority     TEXT,
    job_type      TEXT,
    rule_pass     INTEGER,                   -- 1 = passed deterministic rule filter (P2 scoring/dashboard gate)
    posted_at     TEXT,
    description   TEXT,                      -- plain text, first ~4000 chars (P2 LLM input)
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    content_hash  TEXT,                     -- sha256(title|location|description) — dedup across sources
    status        TEXT NOT NULL DEFAULT 'new',
        -- new | interested | dismissed | packet:drafting | packet:needs_input
        -- | packet:ready | filled | applied | withdrawn
    fit_score     REAL,
    company_score REAL,
    opportunity   REAL,
    final_score   REAL,
    llm_json      TEXT,                    -- tier-1 scoring JSON
    llm_cache_key TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status);
CREATE INDEX IF NOT EXISTS idx_postings_content ON postings(content_hash);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen);

CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    company_id  TEXT,
    kind        TEXT,                      -- funding | hn | blog | search | careers_change
    payload     TEXT,                      -- JSON
    note        TEXT,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS packets (
    id          TEXT PRIMARY KEY,
    posting_id  TEXT NOT NULL REFERENCES postings(id),
    status      TEXT NOT NULL DEFAULT 'drafting',
        -- drafting | needs_input | ready | filled | applied | withdrawn
    dir         TEXT,                      -- applications/<company-slug>-<date>/
    model       TEXT,
    cost_usd    REAL,
    applied_at  TEXT,                      -- when status first hit 'applied'
    decided_at  TEXT,                      -- when offer/rejected landed
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- daily | agent | manual | fill
    started     TEXT NOT NULL,
    finished    TEXT,
    stats       TEXT,                      -- JSON: per-source counts, errors
    cost_usd    REAL NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,            -- e.g. 'hn_last_story'
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tier              TEXT NOT NULL,         -- bulk | agent | quality
    model             TEXT NOT NULL,
    cache_key         TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_day ON llm_calls(tier, date(created_at));

CREATE TABLE IF NOT EXISTS apply_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id     TEXT NOT NULL,
    mode          TEXT NOT NULL,             -- automated (sidecar filler) | assisted (opened for the human)
    status        TEXT NOT NULL,             -- launched|running|submitted|paused|failed|opened
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_apply_runs_packet ON apply_runs(packet_id, id DESC);

CREATE TABLE IF NOT EXISTS email_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT,
    packet_id     TEXT,             -- the packet whose state moved (nullable)
    from_addr     TEXT,
    subject       TEXT,
    sent_at       TEXT,
    message_id    TEXT,
    classification TEXT,            -- interview_invite|offer|rejection|request_more_info|auto_reply|other
    action        TEXT,             -- "state: interviewing" | "draft saved" | "no change"
    detail        TEXT,             -- JSON: draft subject/body, confidence
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_email_events_company ON email_events(company_id, id DESC);

CREATE TABLE IF NOT EXISTS application_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- applied|oa|phone_screen|onsite|note|offer|rejected|follow_up
    event_date  TEXT NOT NULL,             -- YYYY-MM-DD (user-editable)
    title       TEXT,
    notes       TEXT,                      -- interview notes, OA questions, anything
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_app_events_packet
    ON application_events(packet_id, event_date);

CREATE TABLE IF NOT EXISTS outreach (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- cold_email | linkedin
    status      TEXT NOT NULL,             -- running | done | failed
    content     TEXT,                      -- JSON draft (subject/body or contacts)
    error       TEXT,
    model       TEXT,
    cost_usd    REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_company_kind
    ON outreach(company_id, kind);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,          -- sha256(model + prompt) or (content_hash, profile_version)
    response    TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_COUNTED_TABLES = ("companies", "postings", "signals", "packets", "runs", "apply_runs", "llm_cache", "llm_calls", "state")



# ── helpers ──────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "unknown"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── lifecycle ────────────────────────────────────────────────────────────────

# ── lifecycle ────────────────────────────────────────────────────────────────


def connect() -> sqlite3.Connection:
    """Open the DB (read-write). Raises FileNotFoundError if not initialised."""
    path = db_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"database not initialised at {path} — run `jobscout db init`"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> Path:
    """Idempotently create the DB + schema. Returns the DB path."""
    core_paths.ensure_runtime_dirs()
    path = db_path()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    return path


def db_status() -> dict:
    path = db_path()
    if not path.is_file():
        return {"exists": False, "path": str(path), "schema_version": None, "counts": {}}
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        counts: dict[str, int] = {}
        for table in _COUNTED_TABLES:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = -1  # table missing (schema drifted)
        return {
            "exists": True,
            "path": str(path),
            "schema_version": version,
            "expected_version": SCHEMA_VERSION,
            "counts": counts,
            "size_bytes": path.stat().st_size,
        }
    finally:
        conn.close()


# ── companies ────────────────────────────────────────────────────────────────

# ── migration ────────────────────────────────────────────────────────────────


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place column additions for pre-existing DBs (CREATE IF NOT EXISTS
    can't upgrade them). Safe to run repeatedly."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    if "rule_pass" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN rule_pass INTEGER")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "contact_email" not in ccols:
        conn.execute("ALTER TABLE companies ADD COLUMN contact_email TEXT")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(packets)")}
    if "applied_at" not in pcols:
        conn.execute("ALTER TABLE packets ADD COLUMN applied_at TEXT")
    if "decided_at" not in pcols:
        conn.execute("ALTER TABLE packets ADD COLUMN decided_at TEXT")

    # v9: packets.dir → the portable form (relative to var/). Absolute
    # rows from pre-v9 DBs rebase via the /var/applications/ marker —
    # unknown-layout absolutes are left alone (read-time resolution in
    # paths.resolve_packet_dir still handles them).
    for pid, d in conn.execute(
            "SELECT id, dir FROM packets WHERE dir IS NOT NULL"
    ).fetchall():
        if d and d.startswith("/"):
            marker = "/var/applications/"
            if marker in d:
                rel = "applications/" + d.split(marker, 1)[1]
                conn.execute("UPDATE packets SET dir = ? WHERE id = ?",
                             (rel, pid))


# ── P2: rule verdicts, scores, statuses ──────────────────────────────────────
