"""webapp/routers/companies.py — the watchlist substrate view + editor."""

from __future__ import annotations

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.core.schema import WatchlistEntry
from jobscout.webapp import ui
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    boards as extract_boards,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)


def register(app):
    @app.post("/companies/add-url")
    async def companies_add_url(request: Request):
        """Seed a company from any URL: company site, careers page, or an
        ATS board (boards.greenhouse.io/acme, jobs.lever.co/x). Derives the
        domain (or the ATS provider+token), adds a watchlist candidate with
        live ATS probing, and records the URL as the careers page."""
        from urllib.parse import quote as _q
        from urllib.parse import urlparse

        from jobscout.core import watchlist as wlmod

        form = await request.form()
        raw = (form.get("url") or "").strip()
        if not raw:
            return RedirectResponse(
                "/companies?error=" + _q("paste a URL first"),
                status_code=303)
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host:
            return RedirectResponse(
                "/companies?error=" + _q(f"could not read a domain from {raw!r}"),
                status_code=303)

        # ATS board URLs: the path segment IS the board token
        ATS_HOSTS = {"boards.greenhouse.io": "greenhouse",
                     "jobs.lever.co": "lever",
                     "jobs.ashbyhq.com": "ashby",
                     "app.ashbyhq.com": "ashby"}
        provider = ATS_HOSTS.get(host)
        token = (parsed.path or "").strip("/").split("/")[0] if provider else ""

        def registrable(h: str) -> tuple[str, str]:
            labels = h.split(".")
            domain = ".".join(labels[-2:]) if len(labels) >= 2 else h
            name = labels[-2] if len(labels) >= 2 else h
            return domain, name.replace("-", " ").replace("_", " ").title()

        if provider and token:
            name, domain = token.replace("-", " ").title(), None
        else:
            domain, name = registrable(host)

        conn = db.connect()
        try:
            wl = wlmod.load()
            existing = wlmod.find(wl, name)
            if existing is None and domain:
                for tier in ("A", "B", "C", "candidates"):
                    for e in getattr(wl, tier):
                        if (e.domain or "").lower() == (domain or "").lower():
                            existing = (tier, e)
                            break
            if existing is not None:
                return RedirectResponse(
                    "/companies?error=" + _q(
                        f"{existing[1].name} is already on the watchlist "
                        f"({existing[0]}) — edit it instead"),
                    status_code=303)
            ats = ({provider: token} if provider and token
                   else {})
            wlmod.add(wl, WatchlistEntry(
                name=name, domain=domain, ats=ats,
                note=f"seeded from {raw}", found_via="seed-url"),
                "candidates")
            wlmod.save(wl)
            db.upsert_company(
                conn, name=name, domain=domain,
                tier=None, ats_tokens=ats, career_url=raw)
        finally:
            conn.close()
        board_note = f" with ATS board {provider}:{token}" if ats else ""
        return RedirectResponse(
            "/companies?company_saved=1&error=" + _q(
                f"seeded {name}{board_note} as a candidate — "
                "promote it from the editor when it earns it"),
            status_code=303)

    @app.get("/companies/{cid}/outreach", response_class=HTMLResponse)
    def company_outreach_status(request: Request, cid: str):
        """HTMX partial: outreach generation state (polled while running)."""
        from jobscout.webapp.runners import outreach as outreach_runner

        conn = db.connect()
        try:
            st = {k: outreach_runner.status(conn, cid, k)
                  for k in outreach_runner.KINDS}
            company = conn.execute(
                "SELECT contact_email FROM companies WHERE id = ?",
                (cid,)).fetchone()
            contact_email = company["contact_email"] if company else None
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request, "_outreach.html",
            {"cid": cid, "contact_email": contact_email, "st": st},
        )

    @app.post("/companies/{cid}/outreach/{kind}")
    def company_outreach_start(cid: str, kind: str):
        from jobscout.webapp.runners import outreach as outreach_runner

        if kind not in outreach_runner.KINDS:
            return RedirectResponse(f"/companies/{cid}", status_code=303)
        conn = db.connect()
        try:
            outreach_runner.start(cid, kind)
        finally:
            conn.close()
        return RedirectResponse(f"/companies/{cid}?outreach={kind}",
                                status_code=303)

    @app.get("/companies/{cid}", response_class=HTMLResponse)
    def company_edit(request: Request, cid: str, error: str = Query(""),
                     outreach: str = Query("")):
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import watchlist_store

        conn = db.connect()
        try:
            ctx = page_ctx("companies", conn)
            history = db.packets_for_company(conn, cid)
        finally:
            conn.close()
        try:
            tier, entry = watchlist_store.find(cid)
        except watchlist_store.WatchlistStoreError as e:
            return RedirectResponse(
                f"/companies?error={_q(str(e))}", status_code=303)
        outreach_started = ""
        if outreach in ("cold_email", "linkedin"):
            from jobscout.webapp.runners import outreach as outreach_runner

            conn = db.connect()
            try:
                outreach_runner.start(cid, outreach)
                st = {k: outreach_runner.status(conn, cid, k)
                      for k in outreach_runner.KINDS}
                company = conn.execute(
                    "SELECT contact_email FROM companies WHERE id = ?",
                    (cid,)).fetchone()
                contact_email = company["contact_email"] if company else None
            finally:
                conn.close()
            outreach_started = outreach
        else:
            conn = db.connect()
            try:
                from jobscout.webapp.runners import outreach as outreach_runner

                st = {k: outreach_runner.status(conn, cid, k)
                      for k in outreach_runner.KINDS}
                company = conn.execute(
                    "SELECT contact_email FROM companies WHERE id = ?",
                    (cid,)).fetchone()
                contact_email = company["contact_email"] if company else None
            finally:
                conn.close()
        return TEMPLATES.TemplateResponse(
            request, "company_detail.html",
            {**ctx, "cid": cid, "tier": tier, "entry": entry,
             "history": history,
             "tiers": watchlist_store.TIERS, "error": error,
             "outreach_started": outreach_started, "st": st,
             "contact_email": contact_email},
        )

    @app.post("/companies/{cid}/save")
    async def company_save(request: Request, cid: str):
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import watchlist_store

        form = await request.form()
        try:
            watchlist_store.save(
                cid,
                name=form.get("name", ""),
                domain=form.get("domain", ""),
                tier=form.get("tier", "candidates"),
                note=form.get("note", ""),
                ats_text=form.get("ats", ""),
            )
            return RedirectResponse("/companies?company_saved=1",
                                    status_code=303)
        except watchlist_store.WatchlistStoreError as e:
            return RedirectResponse(
                f"/companies/{cid}?error={_q(str(e))}", status_code=303)

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, company_saved: str = Query(""),
                  error: str = Query("")):
        conn = db.connect()
        try:
            rows = db.company_summary(conn)
            signals = db.recent_signals(conn, 60)
            kinds = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM signals GROUP BY kind ORDER BY n DESC"
            ).fetchall()
            ctx = page_ctx("companies", conn)
        finally:
            conn.close()
        row_boards = {r["id"]: extract_boards(r) for r in rows}
        routes = {r["id"]: ui.company_route(r) for r in rows}
        route_counts = ui.route_counts(rows)
        try:
            from jobscout.core.config import load_watchlist

            wl = load_watchlist()
            found_via = {
                db.slugify(e.name): e.found_via
                for tier in ("A", "B", "C", "candidates")
                for e in getattr(wl, tier)
                if e.found_via
            }
        except Exception:               # noqa: BLE001 — provenance optional
            found_via = {}
        return TEMPLATES.TemplateResponse(
            request,
            "companies.html",
            {**ctx, "rows": rows, "boards": row_boards, "routes": routes,
             "route_counts": route_counts, "signals": signals,
             "kinds": kinds, "company_saved": company_saved == "1",
             "error": error, "found_via": found_via},
        )

    # ── discovery ────────────────────────────────────────────────────────
