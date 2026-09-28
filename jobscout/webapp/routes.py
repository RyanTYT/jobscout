"""Dashboard routes: inbox, posting detail, companies, ops (PLAN §7)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobscout import __version__
from jobscout.core import db

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _llm_fields(row) -> dict:
    raw = row["llm_json"] if "llm_json" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _boards(row) -> list[tuple[str, str]]:
    raw = row["ats_tokens"] if "ats_tokens" in row.keys() else None
    if not raw:
        return []
    try:
        return list(json.loads(raw).items())
    except ValueError:
        return []


def create_app() -> FastAPI:
    app = FastAPI(title="jobscout", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    db.init_db()  # idempotent; also migrates schema

    ctx_common = {
        "version": __version__,
    }

    @app.get("/", response_class=HTMLResponse)
    def inbox(
        request: Request,
        status: str = Query("new"),
        tier: str = Query(""),
        q: str = Query(""),
        min_score: str = Query(""),
        all_postings: str = Query(""),
    ):
        conn = db.connect()
        try:
            rows = db.list_postings(
                conn,
                status=None if status == "all" else status,
                tier=tier or None,
                q=q or None,
                min_score=float(min_score) if min_score else None,
                only_rule_pass=not all_postings,
                limit=200,
            )
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request,
            "inbox.html",
            {
                **ctx_common,
                "rows": rows,
                "filters": {
                    "status": status, "tier": tier, "q": q,
                    "min_score": min_score, "all": all_postings,
                },
            },
        )

    @app.get("/posting/{pid}", response_class=HTMLResponse)
    def posting_detail(request: Request, pid: str):
        conn = db.connect()
        try:
            row = db.get_posting(conn, pid)
        finally:
            conn.close()
        if row is None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "posting_detail.html",
            {**ctx_common, "p": row, "llm": _llm_fields(row), "boards": _boards(row)},
        )

    @app.post("/posting/{pid}/status")
    def set_status(pid: str, status: str = Form(...), request: Request = None):
        conn = db.connect()
        try:
            ok = db.set_posting_status(conn, pid, status)
            row = db.get_posting(conn, pid) if ok else None
        finally:
            conn.close()
        if request is not None and any(k.lower().startswith("hx-") for k in request.headers):
            if row is not None:
                return TEMPLATES.TemplateResponse(
                    request, "_row.html", {"request": request, "p": row}
                )
            return HTMLResponse("")
        return RedirectResponse("/", status_code=303)

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request):
        conn = db.connect()
        try:
            rows = db.company_summary(conn)
        finally:
            conn.close()
        boards = {r["id"]: _boards(r) for r in rows}
        signals = db.recent_signals(conn, 25)
        return TEMPLATES.TemplateResponse(
            request,
            "companies.html",
            {**ctx_common, "rows": rows, "boards": boards, "signals": signals},
        )

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request):
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
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request,
            "ops.html",
            {
                **ctx_common,
                "db_status": status,
                "runs": runs_meta,
                "spend": spend,
                "last_stats": last_stats,
            },
        )

    return app
