"""`jobscout run --daily` orchestration (PLAN §4 step 1).

Deterministic only: ATS JSON pulls → dedup → rule filter → markdown digest.
LLM scoring is P2; careers crawl P3; discovery P4/P5.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

from jobscout import digest, watchlist
from jobscout.core import db
from jobscout.core.config import load_profile, load_settings
from jobscout.scoring.rules import guess_seniority, rule_filter
from jobscout.sources.ats import FETCHERS
from jobscout.sources.ats.base import make_client


def run_daily(force: bool = False) -> int:
    settings = load_settings()
    profile = load_profile()
    wl = watchlist.load()

    if settings.discovery.mode == "off":
        print("discovery.mode == off — nothing to do (see config/settings.yaml)")
        return 0

    db.init_db()
    conn = db.connect()

    if not force:
        last = db.last_ok_run(conn, "daily")
        if last:
            age_h = _hours_since(last["started"])
            if age_h is not None and age_h < settings.run.skip_if_run_within_hours:
                print(
                    f"skipping: last good daily run {age_h:.1f}h ago "
                    f"(< {settings.run.skip_if_run_within_hours}h). Use --force to override."
                )
                return 0

    run_id = db.record_run(conn, "daily")
    t0 = time.time()

    entries: list[tuple[str, object]] = (
        [("A", e) for e in wl.A]
        + [("B", e) for e in wl.B]
        + [("C", e) for e in wl.C]
    )

    coverage: list[dict] = []
    errors: list[str] = []
    postings_seen = 0

    client = make_client()
    try:
        for tier, e in entries:
            slug = db.slugify(e.name)
            db.upsert_company(
                conn, name=e.name, domain=e.domain, tier=tier,
                ats_tokens=e.ats or {}, notes=e.note,
            )
            jobs_total = 0
            new_count = 0
            found: dict[str, str] = {}
            for provider, token in (e.ats or {}).items():
                try:
                    postings = FETCHERS[provider](
                        token, client, company=e.name, company_slug=slug
                    )
                except Exception as ex:  # noqa: BLE001 — log and continue with other boards
                    errors.append(f"{e.name}/{provider}:{token}: {ex}")
                    continue
                for p in postings:
                    if not p.seniority:
                        p.seniority = guess_seniority(p.title)
                counts = db.upsert_postings(conn, postings)
                jobs_total += len(postings)
                new_count += counts["new"]
                found[provider] = token
                postings_seen += len(postings)
                time.sleep(0.35)  # one politeness pause per board
            coverage.append(
                {
                    "name": e.name,
                    "tier": tier,
                    "ats": found,
                    "jobs": jobs_total,
                    "new": new_count,
                    "note": e.note or "",
                }
            )
    finally:
        client.close()

    new_rows = db.new_postings_today(conn)
    filtered: list = []
    excluded = 0
    for row in new_rows:
        ns = SimpleNamespace(**dict(row))
        ok, _ = rule_filter(ns, profile)
        db.set_rule_pass(conn, row["id"], ok)
        if ok:
            filtered.append(row)
        else:
            excluded += 1

    closing = db.stale_postings(conn, days=14)

    # ── tier-1 bulk scoring (P2): cheap LLM on rule-pass, cap-respecting ─────
    scored_stats: dict | None = None
    try:
        from jobscout.llm import LlmClient
        from jobscout.scoring import llm_bulk

        llm = LlmClient(conn=conn)
        if llm.available:
            wanted = {r["id"] for r in filtered}
            join_rows = [
                r for r in db.unscored_rule_pass(conn, limit=10_000) if r["id"] in wanted
            ]
            if join_rows:
                scored_stats = llm_bulk.score_postings(conn, join_rows, profile, llm)
        else:
            print(
                "LLM scoring skipped (no API key) — rule-only digest "
                "(set JOBSCOUT_LLM_API_KEY in .env)"
            )
    except Exception as e:  # noqa: BLE001 — scoring must never break the digest
        print(f"LLM scoring failed: {e} — digest is rule-only")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    path = digest.write_daily(
        today,
        coverage=coverage,
        new=filtered,
        excluded_count=excluded,
        errors=errors,
        closing=closing,
        run_meta={
            "run_id": run_id,
            "companies": len(entries),
            "postings_seen": postings_seen,
            "scored": scored_stats,
        },
    )

    db.finish_run(
        conn,
        run_id,
        stats={
            "companies": len(entries),
            "postings_seen": postings_seen,
            "new": len(new_rows),
            "rule_pass": len(filtered),
            "errors": len(errors),
            "scoring": scored_stats,
        },
        cost_usd=(scored_stats or {}).get("cost", 0.0),
        ok=True,
    )
    conn.close()

    print(
        f"daily run done in {time.time() - t0:.1f}s — {len(entries)} companies, "
        f"{postings_seen} postings, {len(filtered)} new rule-pass → {path}"
    )
    if errors:
        print(f"{len(errors)} source errors (see digest)")
    return 0


def _hours_since(iso: str) -> float | None:
    try:
        started = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - started).total_seconds() / 3600
