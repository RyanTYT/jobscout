"""`jobscout run --daily` orchestration (PLAN §4 step 1).

Deterministic only: ATS JSON pulls + careers-page crawl (dark-pool entries) →
dedup → rule filter → LLM scoring (P2, if keyed) → markdown digest.
Discovery is P4/P5.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

from jobscout.core import db, watchlist
from jobscout.core.config import load_profile, load_settings
from jobscout.ops import digest
from jobscout.scoring.rules import guess_seniority, rule_filter
from jobscout.sources.postings import FETCHERS
from jobscout.sources.postings import careers as careers_page
from jobscout.sources.postings.base import make_client


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
    wl_changed = False
    discovery_stats: dict | None = None
    monitoring_stats: dict | None = None

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

            # ── dark-pool entries: careers-page crawl (P3) ──────────────────
            careers_note = ""
            if settings.discovery.pipeline.careers_crawl and not (e.ats or {}):
                try:
                    res = careers_page.crawl_company(e.domain, e.name, client)
                except Exception as ex:  # noqa: BLE001 — careers crawl must never break the run
                    errors.append(f"{e.name}/careers: {ex}")
                    res = None
                if res:
                    if res["board_tokens"]:
                        for prov, tok in res["board_tokens"].items():
                            e.ats.setdefault(prov, tok)
                            found[prov] = tok
                        wl_changed = True
                        careers_note = "ATS board discovered via careers page"
                    elif res["career_url"]:
                        careers_note = "; ".join(res["notes"][:2]) or "careers page tracked"
                    if res["postings"]:
                        for p in res["postings"]:
                            if not p.seniority:
                                p.seniority = guess_seniority(p.title)
                        counts = db.upsert_postings(conn, res["postings"])
                        jobs_total += len(res["postings"])
                        new_count += counts["new"]
                        postings_seen += len(res["postings"])
                    db.upsert_company(
                        conn, name=e.name, domain=e.domain, tier=tier,
                        ats_tokens=(e.ats or None),
                        career_url=res["career_url"],
                        contact_email=res.get("contact_email"),
                        notes=e.note,
                    )

            note = e.note or ""
            if careers_note:
                note = f"{note} — {careers_note}" if note else careers_note
            coverage.append(
                {
                    "name": e.name,
                    "tier": tier,
                    "ats": found,
                    "jobs": jobs_total,
                    "new": new_count,
                    "note": note,
                }
            )
        if wl_changed:
            watchlist.save(wl)
            print("watchlist self-healed: ATS boards discovered via careers pages (see git diff)")

        # ── deterministic discovery (P4+P8): RSS + HN + CSE + GitHub + News + HN-mentions ──
        if settings.discovery.mode != "off":
            try:
                import os as _os

                from jobscout.core.config import load_env as _load_env
                from jobscout.sources import discovery as discovery_mod

                env = {**_os.environ, **_load_env()}
                discovery_stats = discovery_mod.run_sweep(client, conn, wl, profile, settings, env)
                if discovery_stats.get("watchlist_changed"):
                    watchlist.save(wl)
                    n_new = len(discovery_stats.get("candidates") or [])
                    print(f"watchlist grew: +{n_new} candidate(s) from discovery (see git diff)")
            except Exception as e:  # noqa: BLE001 — discovery must never break the run
                errors.append(f"discovery: {e}")

        # ── job sites (keyword search: MyCareersFuture, ...) ───────────
        # role × location queries from the hunting profile — market-wide
        # postings, not just watchlist companies
        if settings.discovery.pipeline.job_sites:
            try:
                from jobscout.sources.postings.sites import sweep_sites

                site_postings = sweep_sites(client, profile)
                for p in site_postings:
                    if not p.seniority:
                        p.seniority = guess_seniority(p.title)
                if site_postings:
                    counts = db.upsert_postings(conn, site_postings)
                    postings_seen += len(site_postings)
                    print(f"job sites: {len(site_postings)} seen, "
                          f"{counts['new']} new")
            except Exception as e:  # noqa: BLE001 — sites must never break the run
                errors.append(f"job-sites: {e}")

        # ── monitoring (P8): careers page changes + sitemap diffs ──────────
        try:
            from jobscout.sources.discovery.monitoring import run_monitoring

            monitoring_stats = run_monitoring(client, conn)
        except Exception as e:  # noqa: BLE001 — monitoring must never break the run
            errors.append(f"monitoring: {e}")
            monitoring_stats = None
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
        from jobscout.clients.llm import LlmClient
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
        discovery=discovery_stats,
        monitoring=monitoring_stats,
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
            "discovery": _discovery_summary(discovery_stats),
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


def _discovery_summary(d: dict | None) -> dict:
    """Compact run-stats summary of a discovery sweep (full detail in digest)."""
    if not d:
        return {}
    out: dict = {}
    for src_key in ("rss", "hn", "cse"):
        part = d.get(src_key)
        if isinstance(part, dict) and "error" not in part and "skipped" not in part:
            out[src_key] = {
                k: part[k] for k in ("feeds", "entries", "signals_new", "comments_matched",
                                     "candidates", "queries", "results")
                if k in part
            }
    if d.get("candidates"):
        out["candidates"] = d["candidates"]
    return out
