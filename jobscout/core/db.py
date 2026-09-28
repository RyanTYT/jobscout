"""SQLite state — the single persistence layer (PLAN §3).

All DB access goes through this module (AGENTS.md rule). WAL mode. Schema is
versioned via PRAGMA user_version so later phases can migrate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jobscout.core.paths import db_path, ensure_runtime_dirs

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          TEXT PRIMARY KEY,          -- slug, e.g. 'janestreet'
    name        TEXT NOT NULL,
    domain      TEXT,
    tier        TEXT CHECK (tier IN ('A','B','C','candidate','dark')),
    ats_tokens  TEXT,                      -- JSON: {greenhouse: token, lever: token, ...}
    career_url  TEXT,
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

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,          -- sha256(model + prompt) or (content_hash, profile_version)
    response    TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_COUNTED_TABLES = ("companies", "postings", "signals", "packets", "runs", "llm_cache", "llm_calls")


# ── helpers ──────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "unknown"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


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
    ensure_runtime_dirs()
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


def upsert_company(
    conn: sqlite3.Connection,
    *,
    name: str,
    domain: str | None = None,
    tier: str | None = None,
    ats_tokens: dict[str, str] | None = None,
    career_url: str | None = None,
    notes: str | None = None,
) -> str:
    slug = slugify(name)
    now = _utcnow()
    conn.execute(
        """
        INSERT INTO companies (id, name, domain, tier, ats_tokens, career_url, non_ats, notes,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            domain = COALESCE(excluded.domain, domain),
            tier = COALESCE(excluded.tier, tier),
            ats_tokens = COALESCE(excluded.ats_tokens, ats_tokens),
            career_url = COALESCE(excluded.career_url, career_url),
            notes = COALESCE(excluded.notes, notes),
            updated_at = excluded.updated_at
        """,
        (
            slug, name, domain, tier, json.dumps(ats_tokens or {}), career_url,
            0 if (ats_tokens) else 1, notes, now, now,
        ),
    )
    conn.commit()
    return slug


# ── postings ─────────────────────────────────────────────────────────────────


def upsert_postings(conn: sqlite3.Connection, postings) -> dict:
    """Dedup by url_hash. Returns {"new": n, "seen": n}.

    first_seen is set once; last_seen refreshes every run a posting still appears.
    Postings that vanish stop refreshing last_seen → stale detection (closing).
    """
    now = _utcnow()
    counts = {"new": 0, "seen": 0}
    for p in postings:
        url_hash = sha256(p.url)
        content_hash = sha256(f"{p.title}|{p.location}|{(p.description or '')[:2000]}")
        row = conn.execute(
            "SELECT id, content_hash FROM postings WHERE url_hash = ?", (url_hash,)
        ).fetchone()
        if row is None:
            pid = f"p_{now[:10]}_{url_hash[:8]}"
            remote = None
            if p.remote is True:
                remote = 1
            elif p.remote is False:
                remote = 0
            conn.execute(
                """
                INSERT INTO postings (id, source, company_id, url, url_hash, title, location,
                                      remote, seniority, job_type, posted_at, description,
                                      first_seen, last_seen, content_hash, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    pid, p.source, p.company_slug, p.url, url_hash, p.title, p.location,
                    remote, p.seniority, p.job_type, p.posted_at, p.description, now, now,
                    content_hash,
                ),
            )
            counts["new"] += 1
        else:
            conn.execute(
                "UPDATE postings SET last_seen = ?, content_hash = ? WHERE id = ?",
                (now, content_hash, row["id"]),
            )
            counts["seen"] += 1
    conn.commit()
    return counts


def new_postings_today(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    today = _utcnow()[:10]
    return conn.execute(
        "SELECT * FROM postings WHERE date(first_seen) = ? ORDER BY company_id, title",
        (today,),
    ).fetchall()


def stale_postings(
    conn: sqlite3.Connection,
    days: int = 14,
    statuses: tuple[str, ...] = ("new", "interested"),
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return conn.execute(
        f"""
        SELECT * FROM postings
        WHERE status IN ({placeholders})
          AND date(last_seen) < date('now', ?)
        ORDER BY last_seen
        """,
        (*statuses, f"-{days} days"),
    ).fetchall()


# ── runs ─────────────────────────────────────────────────────────────────────


def record_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started) VALUES (?, ?)", (kind, _utcnow()))
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: dict,
    cost_usd: float = 0.0,
    ok: bool = True,
) -> None:
    conn.execute(
        "UPDATE runs SET finished = ?, stats = ?, cost_usd = ?, ok = ? WHERE id = ?",
        (_utcnow(), json.dumps(stats), cost_usd, 1 if ok else 0, run_id),
    )
    conn.commit()


def last_ok_run(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE kind = ? AND ok = 1 ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()


# ── migration ────────────────────────────────────────────────────────────────


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place column additions for pre-existing DBs (CREATE IF NOT EXISTS
    can't upgrade them). Safe to run repeatedly."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    if "rule_pass" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN rule_pass INTEGER")


# ── P2: rule verdicts, scores, statuses ──────────────────────────────────────

ALLOWED_STATUSES = ("new", "interested", "dismissed", "withdrawn")


def set_rule_pass(conn: sqlite3.Connection, posting_id: str, passed: bool) -> None:
    conn.execute("UPDATE postings SET rule_pass = ? WHERE id = ?", (1 if passed else 0, posting_id))
    conn.commit()


def update_posting_scores(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    fit: float,
    company_quality: float,
    opportunity: float,
    final: float,
    llm_json: str,
    cache_key: str,
) -> None:
    conn.execute(
        """UPDATE postings SET fit_score = ?, company_score = ?, opportunity = ?,
               final_score = ?, llm_json = ?, llm_cache_key = ? WHERE id = ?""",
        (fit, company_quality, opportunity, final, llm_json, cache_key, posting_id),
    )
    conn.commit()


def set_posting_status(conn: sqlite3.Connection, posting_id: str, status: str) -> bool:
    if status not in ALLOWED_STATUSES:
        return False
    cur = conn.execute(
        "UPDATE postings SET status = ? WHERE id = ?", (status, posting_id)
    )
    conn.commit()
    return cur.rowcount > 0


_POSTING_SELECT = """
SELECT p.*, c.name AS company_name, c.tier, c.non_ats, c.ats_tokens, c.domain AS company_domain
FROM postings p LEFT JOIN companies c ON p.company_id = c.id
"""


def list_postings(
    conn: sqlite3.Connection,
    *,
    status: str | None = "new",
    tier: str | None = None,
    q: str | None = None,
    min_score: float | None = None,
    only_rule_pass: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where, params = [], []
    if status and status != "all":
        where.append("p.status = ?")
        params.append(status)
    if only_rule_pass:
        where.append("p.rule_pass = 1")
    if tier:
        where.append("c.tier = ?")
        params.append(tier)
    if min_score is not None:
        where.append("p.final_score >= ?")
        params.append(min_score)
    if q:
        where.append("(p.title LIKE ? OR c.name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    sql = _POSTING_SELECT
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY COALESCE(p.final_score, -1) DESC, p.first_seen DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def get_posting(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        _POSTING_SELECT + "WHERE p.id = ?", (posting_id,)
    ).fetchone()


def unscored_rule_pass(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Rule-pass, still-eligible postings lacking a final score — tier A first."""
    return conn.execute(
        _POSTING_SELECT
        + """WHERE p.status IN ('new', 'interested') AND p.rule_pass = 1
             AND p.final_score IS NULL
           ORDER BY CASE c.tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END,
                    p.first_seen
           LIMIT ?""",
        (limit,),
    ).fetchall()


# ── P2: dashboard aggregates ─────────────────────────────────────────────────


def company_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, 
               (SELECT COUNT(*) FROM postings p WHERE p.company_id = c.id) AS postings_total,
               (SELECT COUNT(*) FROM postings p WHERE p.company_id = c.id AND p.status = 'new'
                  AND p.rule_pass = 1) AS rule_pass_new
        FROM companies c
        ORDER BY CASE c.tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END, c.name
        """
    ).fetchall()


def llm_spend_by_tier(conn: sqlite3.Connection, days: int = 7) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT tier, model, COUNT(*) AS calls, SUM(prompt_tokens) AS ptok,
               SUM(completion_tokens) AS ctok, SUM(cost_usd) AS cost
        FROM llm_calls
        WHERE date(created_at) >= date('now', ?)
        GROUP BY tier, model ORDER BY tier
        """,
        (f"-{days} days",),
    ).fetchall()


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
