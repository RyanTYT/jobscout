"""core/db/queries.py — every CRUD function over the schema.

Imported and re-exported by core/db (the package root), so callers see
one `db` module exactly as before the split.
"""

from __future__ import annotations

import json
import sqlite3

from jobscout.core.db.schema import _utcnow, sha256, slugify

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
# ── P2: rule verdicts, scores, statuses ──────────────────────────────────────

ALLOWED_STATUSES = (
    "new", "interested", "dismissed", "withdrawn",
    "packet:drafting", "packet:needs_input", "packet:ready",
    "filled", "applied",
)


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


# sorting options for list_postings (webapp inbox)
_SORTS = {
    "newest": "p.first_seen DESC, COALESCE(p.posted_at, '') DESC",
    "oldest": "p.first_seen ASC, COALESCE(p.posted_at, '') ASC",
    "score": "COALESCE(p.final_score, -1) DESC, p.first_seen DESC",
    "score_asc": "p.final_score IS NULL, p.final_score ASC, p.first_seen DESC",
    "posted": "COALESCE(p.posted_at, '') DESC",
    "company": "c.name COLLATE NOCASE, p.first_seen DESC",
    "company_desc": "c.name COLLATE NOCASE DESC, p.first_seen DESC",
    "title": "p.title COLLATE NOCASE, p.first_seen DESC",
    "title_desc": "p.title COLLATE NOCASE DESC, p.first_seen DESC",
    "location": "COALESCE(p.location, '') COLLATE NOCASE, p.first_seen DESC",
    "location_desc": "COALESCE(p.location, '') COLLATE NOCASE DESC, p.first_seen DESC",
    "level": "COALESCE(p.seniority, 'zzz') COLLATE NOCASE, p.first_seen DESC",
    "level_desc": "COALESCE(p.seniority, '') COLLATE NOCASE DESC, p.first_seen DESC",
}


def _posting_filters(
    *,
    status: str | None,
    tier: str | None,
    q: str | None,
    min_score: float | None,
    only_rule_pass: bool,
    level: str | None,
    location: str | None,
    company: str | None,
    source: str | None,
    remote: str | None,
) -> tuple[str, list]:
    """Shared WHERE builder for list_postings and count_postings.

    Returns ("WHERE ... " or "", params). Parameterised — no injection.
    """
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
    if level:
        if level == "unknown":
            where.append("COALESCE(p.seniority, '') = ''")
        else:
            where.append("p.seniority = ?")
            params.append(level)
    if location:
        where.append("p.location LIKE ?")
        params.append(f"%{location}%")
    if company:
        where.append("p.company_id = ?")
        params.append(company)
    if source:
        if source == "careers":
            where.append("p.source NOT LIKE 'ats:%'")
        else:
            where.append("p.source LIKE ?")
            params.append(f"ats:{source}%")
    if remote == "1":
        where.append("p.remote = 1")
    elif remote == "0":
        where.append("COALESCE(p.remote, 0) = 0")
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    return clause, params


def list_postings(
    conn: sqlite3.Connection,
    *,
    status: str | None = "new",
    tier: str | None = None,
    q: str | None = None,
    min_score: float | None = None,
    only_rule_pass: bool = True,
    level: str | None = None,
    location: str | None = None,
    company: str | None = None,
    source: str | None = None,
    remote: str | None = None,
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Filtered posting list. `source` takes an ATS name (greenhouse, lever,
    ashby, smartrecruiters) or 'careers' for crawled postings; sort is one of
    _SORTS; level 'unknown' selects rows without a seniority guess."""
    clause, params = _posting_filters(
        status=status, tier=tier, q=q, min_score=min_score,
        only_rule_pass=only_rule_pass, level=level, location=location,
        company=company, source=source, remote=remote,
    )
    order = _SORTS.get(sort, _SORTS["score"])
    sql = _POSTING_SELECT + clause
    sql += f"ORDER BY {order} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def count_postings(
    conn: sqlite3.Connection,
    *,
    status: str | None = "new",
    tier: str | None = None,
    q: str | None = None,
    min_score: float | None = None,
    only_rule_pass: bool = True,
    level: str | None = None,
    location: str | None = None,
    company: str | None = None,
    source: str | None = None,
    remote: str | None = None,
    sort: str = "score",
) -> int:
    """Count of postings matching the same filters as list_postings.

    `sort` is accepted so the same kwargs dict can drive both calls; the
    ordering is irrelevant for a count.
    """
    clause, params = _posting_filters(
        status=status, tier=tier, q=q, min_score=min_score,
        only_rule_pass=only_rule_pass, level=level, location=location,
        company=company, source=source, remote=remote,
    )
    sql = ("SELECT COUNT(*) FROM postings p JOIN companies c ON c.id = p.company_id "
           + clause)
    return conn.execute(sql, params).fetchone()[0]


def filter_options(conn: sqlite3.Connection) -> dict:
    """Distinct filter values with counts, for the inbox filter panel."""
    levels = conn.execute(
        "SELECT COALESCE(NULLIF(p.seniority, ''), 'unknown') AS lvl, COUNT(*) AS n "
        "FROM postings p WHERE p.rule_pass = 1 GROUP BY lvl ORDER BY n DESC"
    ).fetchall()
    companies = conn.execute(
        "SELECT c.id, c.name, c.tier, COUNT(p.id) AS n "
        "FROM companies c JOIN postings p ON p.company_id = c.id "
        "WHERE p.rule_pass = 1 "
        "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    ).fetchall()
    sources = conn.execute(
        "SELECT CASE WHEN source LIKE 'ats:%' THEN substr(source, 5, "
        "instr(substr(source, 5) || ':', ':') - 1) ELSE 'careers' END AS src, "
        "COUNT(*) AS n FROM postings p WHERE p.rule_pass = 1 "
        "GROUP BY src ORDER BY n DESC"
    ).fetchall()
    statuses = conn.execute(
        "SELECT status, COUNT(*) AS n FROM postings WHERE rule_pass = 1 "
        "GROUP BY status ORDER BY n DESC"
    ).fetchall()
    return {
        "levels": [(r["lvl"], r["lvl"], r["n"]) for r in levels],
        "companies": [(r["id"], r["name"], r["n"]) for r in companies],
        "sources": [(r["src"], r["src"], r["n"]) for r in sources],
        "statuses": [(r["status"], r["status"], r["n"]) for r in statuses],
    }


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
                  AND p.rule_pass = 1) AS rule_pass_new,
               (SELECT COUNT(*) FROM signals s WHERE s.company_id = c.id) AS signal_count
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


# ── P4: signals + state ──────────────────────────────────────────────────────


def upsert_signal(
    conn: sqlite3.Connection,
    *,
    kind: str,
    key: str,
    company_id: str | None = None,
    payload: dict | None = None,
    note: str | None = None,
) -> bool:
    """Idempotent by (kind, key). Returns True when a NEW signal was stored."""
    sid = sha256(f"{kind}|{key}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO signals (id, company_id, kind, payload, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, company_id, kind, json.dumps(payload or {}), note),
    )
    conn.commit()
    return cur.rowcount > 0


def recent_signals(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.*, c.name AS company_name FROM signals s "
        "LEFT JOIN companies c ON s.company_id = c.id "
        "ORDER BY s.seen_at DESC, s.id LIMIT ?",
        (limit,),
    ).fetchall()


def signals_for_company(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE company_id = ? ORDER BY seen_at DESC LIMIT 20",
        (company_id,),
    ).fetchall()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )
    conn.commit()


# ── P6: packets ───────────────────────────────────────────────────────────────

PACKET_STATUSES = (
    "drafting", "needs_input", "ready", "filled", "applied", "withdrawn",
)


def upsert_packet(
    conn: sqlite3.Connection,
    *,
    packet_id: str,
    posting_id: str,
    status: str,
    dir_path: str | None = None,
    model: str | None = None,
    cost_usd: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO packets (id, posting_id, status, dir, model, cost_usd,
                             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            dir = COALESCE(excluded.dir, dir),
            model = COALESCE(excluded.model, model),
            cost_usd = cost_usd + excluded.cost_usd,
            updated_at = excluded.updated_at
        """,
        (packet_id, posting_id, status, dir_path, model, cost_usd),
    )
    conn.commit()


def get_packet(conn: sqlite3.Connection, packet_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM packets WHERE id = ?", (packet_id,)
    ).fetchone()


def get_packet_for_posting(
    conn: sqlite3.Connection, posting_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM packets WHERE posting_id = ? ORDER BY updated_at DESC LIMIT 1",
        (posting_id,),
    ).fetchone()


def set_packet_status(conn: sqlite3.Connection, packet_id: str, status: str) -> bool:
    if status not in PACKET_STATUSES:
        return False
    cur = conn.execute(
        "UPDATE packets SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, packet_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ── apply runs (webapp/apply.py launcher) ───────────────────────────────────


def record_apply_run(conn: sqlite3.Connection, *, packet_id: str, mode: str,
                      status: str, detail: str | None = None) -> int:
    now = _utcnow()
    cur = conn.execute(
        "INSERT INTO apply_runs (packet_id, mode, status, detail, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (packet_id, mode, status, detail, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_apply_run(conn: sqlite3.Connection, *, packet_id: str, status: str,
                     detail: str | None = None) -> int:
    """Latest run for a packet moves to `status` (runs are per-launch)."""
    now = _utcnow()
    cur = conn.execute(
        "UPDATE apply_runs SET status = ?, detail = COALESCE(?, detail), updated_at = ?"
        " WHERE id = (SELECT id FROM apply_runs WHERE packet_id = ? ORDER BY id DESC LIMIT 1)",
        (status, detail, now, packet_id),
    )
    conn.commit()
    return cur.rowcount


def list_apply_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.*, p.title AS posting_title, c.name AS company_name"
        " FROM apply_runs r"
        " LEFT JOIN packets pk ON pk.id = r.packet_id"
        " LEFT JOIN postings p ON p.id = pk.posting_id"
        " LEFT JOIN companies c ON c.id = p.company_id"
        " ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def list_packets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT pk.*, p.title AS posting_title, p.company_id, p.url AS posting_url,
               c.name AS company_name, c.tier
        FROM packets pk
        LEFT JOIN postings p ON pk.posting_id = p.id
        LEFT JOIN companies c ON p.company_id = c.id
        ORDER BY pk.updated_at DESC
        """
    ).fetchall()
