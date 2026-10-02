"""webapp/routers/discovery.py — agent control: mode, caps, targeting, hunts."""

from __future__ import annotations

import json

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)
from jobscout.webapp.runners import agent_runner


def _sidecar_available() -> bool:
    """True when the JobPilot sidecar is built and usable."""
    try:
        from jobscout.clients.sidecar import SidecarClient

        return SidecarClient.available()
    except Exception:                             # noqa: BLE001 — never break the page
        return False


def _sidecar_path() -> str:
    try:
        from jobscout.clients.sidecar import SidecarClient

        return str(SidecarClient._default_path())
    except Exception:                             # noqa: BLE001
        return ""


def register(app):
    @app.get("/discovery", response_class=HTMLResponse)
    def discovery_page(request: Request, ran: str = Query(""),
                       started: str = Query(""),
                       saved: str = Query(""), error: str = Query(""),
                       caps_saved: str = Query(""),
                       sweep_saved: str = Query("")):
        from jobscout.core.config import load_settings
        from jobscout.webapp.stores import targeting_store

        settings = load_settings()
        conn = db.connect()
        try:
            agent_spend = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
                "FROM llm_calls WHERE tier = 'agent' "
                "AND date(created_at) >= date('now', '-30 days')"
            ).fetchone()
            last_agent_run = conn.execute(
                "SELECT * FROM runs WHERE kind = 'agent' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            recent_rows = db.recent_runs(conn, 5)
            recent_runs = []
            for r in recent_rows:
                try:
                    st = json.loads(r["stats"] or "{}")
                except ValueError:
                    st = {}
                row = dict(r)
                row["stats"] = st
                recent_runs.append(row)
            scoring_state = db.get_scoring_state(conn)
            ctx = page_ctx("discovery", conn)
        finally:
            conn.close()
        from jobscout.core.config import load_profile, profile_hash

        try:
            profile = load_profile()
            target = profile.target
            phash = profile_hash(profile)
        except Exception:                 # noqa: BLE001 — profile optional here
            from jobscout.core.schema import TargetCfg

            target = TargetCfg()
            phash = None
        scoring_summary = {"hash": phash, "state": scoring_state}
        # Run reports (daily + morning, with the promote table) render in the
        # split-panel browser below — served by /discovery/reports off the
        # runtime root. This page used to carry a second, read-only "Morning
        # reports" card duplicating it; that is gone.
        return TEMPLATES.TemplateResponse(
            request,
            "discovery.html",
            {
                **ctx,
                "settings": settings,
                "target": target,
                "level_tokens": targeting_store.LEVEL_TOKENS,
                "remote_prefs": targeting_store.REMOTE_PREFS,
                "targeting_saved": saved == "1",
                "targeting_error": error,
                "agent_spend": agent_spend,
                "last_agent_run": last_agent_run,
                "recent_runs": recent_runs,
                "just_started": started == "1",
                "caps_saved": caps_saved == "1",
                "sweep_saved": sweep_saved == "1",
                "pipeline": settings.discovery.pipeline,
                "profile_hash": scoring_summary["hash"],
                "score_state": scoring_summary["state"],
                "batch_size": settings.discovery.pipeline.llm_batch_size,
                # the sidecar is what fills the bot-walled boards; without it
                # LinkedIn / Indeed / Wellfound / YC silently return nothing,
                # which looks like "no new jobs" rather than a broken source.
                "sidecar_ok": _sidecar_available(),
                "sidecar_path": _sidecar_path(),
                "running": agent_runner.running(),
                "run_out": agent_runner.state()["out"],
                "run_error": agent_runner.state()["error"],
                "focus": agent_runner.state()["focus"],
            },
        )

    @app.post("/discovery/mode")
    def set_mode(mode: str = Form(...)):
        from urllib.parse import quote as _q

        from jobscout.core.config import set_discovery_mode

        try:
            set_discovery_mode(mode)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(
                f"/discovery?error={_q(f'mode change failed: {e}')}",
                status_code=303)
        return RedirectResponse("/discovery", status_code=303)

    @app.post("/discovery/run")
    def run_agent_now(focus: str = Form("")):
        """Full hunt (focus="") chains run --daily + the morning agent;
        a profile hunt runs the agent alone. Background thread — the
        button returns immediately (agent_runner owns the lifecycle)."""
        agent_runner.launch(focus)
        return RedirectResponse("/discovery?started=1", status_code=303)

    @app.post("/discovery/run/cancel")
    def run_agent_cancel():
        """Kill the in-flight agent run (the button on the run panel)."""
        agent_runner.cancel()
        return RedirectResponse("/discovery?started=1", status_code=303)

    @app.get("/discovery/run-status", response_class=HTMLResponse)
    def discovery_run_status(request: Request):
        """HTMX partial: the agent-run panel — button state, live badge,
        agent spend (updated while the run is in flight), then output."""
        conn = db.connect()
        try:
            agent_spend = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
                "FROM llm_calls WHERE tier = 'agent' "
                "AND date(created_at) >= date('now', '-30 days')"
            ).fetchone()
            # this partial re-renders every 3s while a run is in flight, so it
            # is the only place live scoring progress can surface
            score_state = db.get_scoring_state(conn)
        finally:
            conn.close()
        st = agent_runner.state()
        from jobscout.core.config import load_settings
        return TEMPLATES.TemplateResponse(
            request, "_run_status.html",
            {"running": st["running"], "run_out": st["out"],
             "run_error": st["error"], "focus": st["focus"],
             "agent_spend": agent_spend, "score_state": score_state,
             "batch_size": load_settings().discovery.pipeline.llm_batch_size},
        )

    # ── the report browser ───────────────────────────────────────────────────
    # _report_tabs.html / _report_viewer.html call these; without them the
    # templates are dead files and the Discovery page shows no reports.

    @app.get("/discovery/reports", response_class=HTMLResponse)
    def report_list(request: Request):
        """HTMX partial: the split-panel report browser (sidebar + viewer)."""
        from jobscout.webapp import reports

        conn = db.connect()
        try:
            ctx = reports.list_reports(conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(request, "_report_tabs.html", ctx)

    @app.get("/discovery/reports/{kind}/{date}", response_class=HTMLResponse)
    def report_view(request: Request, kind: str, date: str):
        """HTMX partial: one report, loaded into #report-content."""
        from jobscout.webapp import reports

        conn = db.connect()
        try:
            try:
                ctx = {"report": reports.load_report(conn, kind, date)}
            except reports.ReportError as e:
                ctx = {"kind": kind, "date": date, "error": str(e)}
        finally:
            conn.close()
        name = ("_report_viewer.html" if "report" in ctx
                else "_report_not_found.html")
        return TEMPLATES.TemplateResponse(request, name, ctx)

    @app.post("/discovery/reports/{kind}/{date}/promote",
              response_class=HTMLResponse)
    async def report_promote(kind: str, date: str, request: Request):
        """Move checked companies into a watchlist tier. Writes watchlist.yaml
        and the DB tier together; the reply lands in #promote-result."""
        from jobscout.webapp.stores import watchlist_store

        form = await request.form()
        checked = form.getlist("company")
        tiers = {
            slug: (form.get(f"tier_{slug}") or "").strip()
            for slug in checked
        }
        try:
            result = watchlist_store.promote_many(
                {db.slugify(slug): t for slug, t in tiers.items() if t})
        except watchlist_store.WatchlistStoreError as e:
            return TEMPLATES.TemplateResponse(
                request, "_promote_result.html",
                {"ok": False, "message": str(e)}, status_code=422)

        return TEMPLATES.TemplateResponse(
            request, "_promote_result.html",
            {"ok": True, "promoted": result["promoted"],
             "skipped": result["skipped"]})

    @app.post("/discovery/rescore")
    def rescore_now():
        """Rescore postings — batched LLM scoring only, no sweep and no agent.

        scope="auto" inside score_unscored: a full pass when the profile hash
        has moved since the last completed pass, otherwise only never-scored
        postings. Resumable — an interrupted pass continues where it stopped.
        """
        agent_runner.launch("rescore")
        return RedirectResponse("/discovery?started=1", status_code=303)

    @app.post("/discovery/pipeline")
    async def discovery_pipeline_save(request: Request):
        """The daily-sweep knob: which deterministic sources run, how
        many CSE queries, whether job-site searches (MCF...) run."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import settings_store

        form = await request.form()
        try:
            settings_store.save_pipeline(
                ats_boards=form.get("ats_boards") == "1",
                careers_crawl=form.get("careers_crawl") == "1",
                rss=form.get("rss") == "1",
                job_sites=form.get("job_sites") == "1",
                cse_queries=form.get("cse_queries", "10"),
            )
            return RedirectResponse("/discovery?sweep_saved=1",
                                    status_code=303)
        except settings_store.SettingsStoreError as e:
            return RedirectResponse(
                f"/discovery?error={_q(str(e))}", status_code=303)

    @app.post("/discovery/agent-caps")
    async def discovery_agent_caps(request: Request):
        """Agent caps editor: schedule, step cap, cost cap, run-on-signal."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import settings_store

        form = await request.form()
        try:
            settings_store.save(
                schedule=form.get("schedule", "weekdays"),
                max_steps=form.get("max_steps", "60"),
                max_cost_usd=form.get("max_cost_usd", "0.50"),
                run_on_signal=form.get("run_on_signal") == "1",
            )
            return RedirectResponse("/discovery?caps_saved=1",
                                    status_code=303)
        except settings_store.SettingsStoreError as e:
            return RedirectResponse(
                f"/discovery?error={_q(str(e))}", status_code=303)

    @app.post("/discovery/targeting")
    async def discovery_targeting_save(request: Request):
        """Hunting-profile edits: line-patched into config/profile.yaml
        with validation + rollback; version bump re-scores the LLM cache."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import targeting_store

        form = await request.form()
        try:
            result = targeting_store.save(
                seniorities=form.getlist("seniorities"),
                primary_locations=form.get("primary_locations", ""),
                other_locations=form.get("other_locations", ""),
                roles=form.get("roles", ""),
                stack=form.get("stack", ""),
                remote_preference=form.get("remote_preference", "hybrid"),
                remote_allowed=form.get("remote_allowed") == "1",
            )
            return RedirectResponse(
                f"/discovery?saved=1&v={result['profile_version']}",
                status_code=303)
        except targeting_store.TargetingError as e:
            return RedirectResponse(
                f"/discovery?error={_q(str(e))}", status_code=303)
