"""webapp/routers/discovery.py — agent control: mode, caps, targeting, hunts."""

from __future__ import annotations

import json

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp import agent_runner
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)


def register(app):
    @app.get("/discovery", response_class=HTMLResponse)
    def discovery_page(request: Request, ran: str = Query(""),
                       started: str = Query(""),
                       saved: str = Query(""), error: str = Query(""),
                       caps_saved: str = Query("")):
        from jobscout.core.config import load_settings
        from jobscout.webapp import targeting_store

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
            ctx = page_ctx("discovery", conn)
        finally:
            conn.close()
        from jobscout.core.config import load_profile

        try:
            target = load_profile().target
        except Exception:                 # noqa: BLE001 — profile optional here
            from jobscout.core.schema import TargetCfg

            target = TargetCfg()
        from jobscout.core import paths as core_paths

        # morning_reports live under the runtime root (JOBSCOUT_HOME in the
        # packaged app) — NOT relative to the webapp templates dir, which
        # pointed inside the frozen bundle and showed an empty report list
        reports = sorted(core_paths.morning_reports_dir().glob("*.md"))
        latest_report = None
        if reports:
            latest_report = {"name": reports[-1].name,
                             "content": reports[-1].read_text(encoding="utf-8")[:6000]}
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
                "reports": [r.name for r in reports[-10:]],
                "latest_report": latest_report,
                "recent_runs": recent_runs,
                "just_started": started == "1",
                "caps_saved": caps_saved == "1",
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
        finally:
            conn.close()
        st = agent_runner.state()
        return TEMPLATES.TemplateResponse(
            request, "_run_status.html",
            {"running": st["running"], "run_out": st["out"],
             "run_error": st["error"], "focus": st["focus"],
             "agent_spend": agent_spend},
        )

    @app.post("/discovery/agent-caps")
    async def discovery_agent_caps(request: Request):
        """Agent caps editor: schedule, step cap, cost cap, run-on-signal."""
        from urllib.parse import quote as _q

        from jobscout.webapp import settings_store

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

        from jobscout.webapp import targeting_store

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
