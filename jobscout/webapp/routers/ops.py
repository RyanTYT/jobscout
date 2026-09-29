"""webapp/routers/ops.py — observability: spend, models, credentials, search engine."""

from __future__ import annotations

import json

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)
from jobscout.webapp.runners import agent_runner


def register(app):
    @app.post("/open-url")
    async def open_url(request: Request):
        """Open an external URL in the DEFAULT browser (OS `open`).

        Called by the webview for company links, careers buttons, signal
        URLs — browser-opening from a Tauri webview is unreliable (plugin
        permissions, target=_blank is a no-op in WKWebView), so the
        backend does it on the same machine it serves. http/https only."""
        import subprocess
        import sys
        from urllib.parse import urlparse

        form = await request.form()
        url = (form.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"error": "only http/https URLs can be opened"}
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, url], start_new_session=True)
            return {"ok": True}
        except OSError as e:
            return {"error": f"failed to open: {e}"}

    @app.get("/ops/spend", response_class=HTMLResponse)
    def ops_spend(request: Request):
        """HTMX partial: the ops spend table, polled while the agent runs."""
        conn = db.connect()
        try:
            spend = db.llm_spend_by_tier(conn, days=7)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request, "_ops_spend.html",
            {"spend": spend, "running": agent_runner.running()},
        )

    # ── applications / packets ───────────────────────────────────────────

    @app.post("/ops/search-provider")
    async def ops_search_provider(request: Request):
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import settings_store

        form = await request.form()
        try:
            settings_store.save_search(
                provider=form.get("provider", "auto"),
                llm_model=form.get("llm_model", ""),
            )
            return RedirectResponse("/ops?search_saved=1", status_code=303)
        except settings_store.SettingsStoreError as e:
            return RedirectResponse(
                f"/ops?key_error={_q(str(e))}", status_code=303)

    @app.post("/ops/models")
    async def ops_models_save(request: Request):
        """models.yaml editor: tier models + prices + caps, with rollback."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import models_store

        form = await request.form()
        tiers = {
            t: {
                "model": form.get(f"model_{t}", ""),
                "price_in": form.get(f"price_in_{t}", ""),
                "price_out": form.get(f"price_out_{t}", ""),
                "max_daily_usd": form.get(f"cap_{t}", ""),
            }
            for t in models_store.TIERS
        }
        caps = {"monthly_usd": form.get("monthly_usd", ""),
                "on_cap": form.get("on_cap", "rule-only")}
        try:
            models_store.save(tiers=tiers, caps=caps)
            return RedirectResponse("/ops?models_saved=1", status_code=303)
        except models_store.ModelsStoreError as e:
            return RedirectResponse(
                f"/ops?models_error={_q(str(e))}", status_code=303)

    @app.post("/ops/models/refresh-prices")
    def ops_models_refresh():
        """Fetch live per-token prices from the provider's /models list
        (OpenRouter-style) and update models.yaml prices only."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import models_store

        try:
            result = models_store.refresh_prices()
        except models_store.ModelsStoreError as e:
            result = {"error": str(e)}
        if result.get("error"):
            return RedirectResponse(
                f"/ops?models_error={_q(result['error'])}", status_code=303)
        note = "; ".join(result.get("updated") or [])
        if result.get("missing"):
            note += " — no match: " + ", ".join(result["missing"])
        return RedirectResponse(
            f"/ops?models_refreshed={_q(note or 'prices refreshed')}",
            status_code=303)

    @app.post("/ops/cse-keys")
    async def ops_cse_save(request: Request):
        """Search keys (JOBSCOUT_CSE_KEY / JOBSCOUT_CSE_CX) into .env."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import key_store

        form = await request.form()
        try:
            key_store.save_cse(key=form.get("cse_key", ""),
                               cx=form.get("cse_cx", ""))
            return RedirectResponse("/ops?key_saved=1", status_code=303)
        except key_store.KeyStoreError as e:
            return RedirectResponse(
                f"/ops?key_error={_q(str(e))}", status_code=303)

    @app.post("/ops/cse-keys/clear")
    def ops_cse_clear():
        from jobscout.webapp.stores import key_store

        key_store.clear_cse()
        return RedirectResponse("/ops?key_saved=1", status_code=303)

    @app.post("/ops/brave-key")
    async def ops_brave_save(request: Request):
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import key_store

        form = await request.form()
        try:
            key_store.save_brave(form.get("brave_key", ""))
            return RedirectResponse("/ops?key_saved=1", status_code=303)
        except key_store.KeyStoreError as e:
            return RedirectResponse(
                f"/ops?key_error={_q(str(e))}", status_code=303)

    @app.post("/ops/brave-key/clear")
    def ops_brave_clear():
        from jobscout.webapp.stores import key_store

        key_store.clear_brave()
        return RedirectResponse("/ops?key_saved=1", status_code=303)

    @app.post("/ops/llm-key")
    async def ops_llm_key(request: Request):
        """Save the LLM API key into .env (gitignored; live without restart).
        The key itself is never rendered back — only a masked tail."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import key_store

        form = await request.form()
        try:
            key_store.save_key(form.get("api_key", ""))
            return RedirectResponse("/ops?key_saved=1", status_code=303)
        except key_store.KeyStoreError as e:
            return RedirectResponse(
                f"/ops?key_error={_q(str(e))}", status_code=303)

    @app.post("/ops/llm-key/clear")
    async def ops_llm_key_clear(request: Request):
        from jobscout.webapp.stores import key_store

        key_store.clear_key()
        return RedirectResponse("/ops?key_saved=1", status_code=303)

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request, key_saved: str = Query(""),
            key_error: str = Query(""), models_saved: str = Query(""),
            models_error: str = Query(""),
            models_refreshed: str = Query(""),
            search_saved: str = Query("")):
        from jobscout.core import config as core_config
        from jobscout.webapp.stores import key_store, models_store

        try:
            cse_status = key_store.cse_status()
        except Exception:                       # noqa: BLE001
            cse_status = {"key_set": False, "key_tail": "", "cx": ""}
        try:
            brave_status = key_store.brave_status()
        except Exception:                       # noqa: BLE001
            brave_status = {"key_set": False, "key_tail": ""}
        try:
            models_cfg = models_store.current()
        except Exception:                       # noqa: BLE001
            models_cfg = None
        try:
            llm_status = key_store.status()
        except Exception:               # noqa: BLE001 — status is informational
            llm_status = {"key_set": False, "key_tail": "", "base_url": "",
                          "key_env": "JOBSCOUT_LLM_API_KEY",
                          "base_url_env": "JOBSCOUT_LLM_BASE_URL"}
        conn = db.connect()
        try:
            status = db.db_status()
            runs = db.recent_runs(conn)
            spend = db.llm_spend_by_tier(conn, days=7)
            last = runs[0] if runs else None
            last_stats = {}
            if last is not None and last["stats"]:
                try:
                    last_stats = json.loads(last["stats"])
                except ValueError:
                    last_stats = {}
            runs_meta = []
            for r in runs:
                try:
                    st = json.loads(r["stats"] or "{}")
                except ValueError:
                    st = {}
                runs_meta.append({"row": r, "stats": st})
            ctx = page_ctx("ops", conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request,
            "ops.html",
            {
                **ctx,
                "db_status": status,
                "runs": runs_meta,
                "spend": spend,
                "last_stats": last_stats,
            
                "llm_status": llm_status,
                "cse_status": cse_status,
                "brave_status": brave_status,
                "search_cfg": (core_config.load_settings().search
                               if True else None),
                "search_saved": search_saved == "1",
                "models_cfg": models_cfg,
                "models_saved": models_saved == "1",
                "models_error": models_error,
                "models_refreshed": models_refreshed,
                "running": agent_runner.running(),
                "key_saved": key_saved == "1",
                "key_error": key_error,},
        )

    # ── profile (master resume editor) ───────────────────────────────────
