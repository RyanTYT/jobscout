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
_run_output_holder: dict = {}


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

    @app.get("/discovery", response_class=HTMLResponse)
    def discovery_page(request: Request, ran: str = Query("")):
        from jobscout.core.config import load_settings

        settings = load_settings()
        conn = db.connect()
        try:
            agent_spend = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
                "FROM llm_calls WHERE tier = 'agent' AND date(created_at) >= date('now', '-30 days')"
            ).fetchone()
            last_agent_run = conn.execute(
                "SELECT * FROM runs WHERE kind = 'agent' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        reports = sorted(BASE_DIR.parents[1].joinpath("morning_reports").glob("*.md"))
        latest_report = None
        if reports:
            latest_report = {"name": reports[-1].name,
                             "content": reports[-1].read_text(encoding="utf-8")[:6000]}
        return TEMPLATES.TemplateResponse(
            request,
            "discovery.html",
            {
                **ctx_common,
                "settings": settings,
                "agent_spend": agent_spend,
                "last_agent_run": last_agent_run,
                "reports": [r.name for r in reports[-10:]],
                "latest_report": latest_report,
                "run_output": _run_output_holder.get("out") if ran == "1" else None,
            },
        )

    @app.post("/discovery/mode")
    def set_mode(mode: str = Form(...), request: Request = None):
        from jobscout.core.config import set_discovery_mode

        try:
            set_discovery_mode(mode)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"failed: {e}", status_code=400)
        return RedirectResponse("/discovery", status_code=303)

    @app.post("/discovery/run")
    def run_agent_now(request: Request = None):
        import subprocess
        import sys

        cli = str(Path(sys.executable).parent / "jobscout")
        try:
            proc = subprocess.run(
                [cli, "agent"], capture_output=True, text=True, timeout=900
            )
            _run_output_holder["out"] = ((proc.stdout or "") + (proc.stderr or ""))[-8000:]
        except subprocess.TimeoutExpired:
            _run_output_holder["out"] = "run timed out after 900s (caps should prevent this)"
        return RedirectResponse("/discovery?ran=1", status_code=303)

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request):
        conn = db.connect()
        try:
            packets = db.list_packets(conn)
        finally:
            conn.close()
        manifests = []
        for pk in packets:
            manifest = {}
            if pk["dir"]:
                mp = Path(pk["dir"]) / "packet.yaml"
                if mp.is_file():
                    try:
                        import yaml as _yaml

                        manifest = _yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
                    except ValueError:
                        manifest = {}
            manifests.append({"row": pk, "manifest": manifest})
        return TEMPLATES.TemplateResponse(
            request, "applications.html",
            {**ctx_common, "packets": manifests},
        )

    @app.get("/packet/{pid}", response_class=HTMLResponse)
    def packet_detail(request: Request, pid: str):
        conn = db.connect()
        try:
            pk = db.get_packet(conn, pid)
            posting = db.get_posting(conn, pk["posting_id"]) if pk else None
        finally:
            conn.close()
        if pk is None:
            return RedirectResponse("/applications", status_code=303)
        import yaml as _yaml

        def _load(name):
            fp = Path(pk["dir"] or "") / name
            if not fp.is_file():
                return None
            try:
                text = fp.read_text(encoding="utf-8")
                return _yaml.safe_load(text) if name.endswith(".yaml") else text
            except (ValueError, OSError):
                return None

        return TEMPLATES.TemplateResponse(
            request, "packet_detail.html",
            {
                **ctx_common,
                "pk": pk,
                "posting": posting,
                "manifest": _load("packet.yaml") or {},
                "fill_sheet": (_load("fill_sheet.yaml") or {}).get("fields", []),
                "claims": _load("claim_check.yaml") or [],
                "resume_md": _load("resume.md"),
                "cover_letter": _load("cover_letter.md"),
                "tailor": _load("tailor.yaml") or {},
            },
        )

    @app.post("/posting/{pid}/prepare")
    def prepare_now(pid: str, dry_run: bool = Form(False)):
        from jobscout.packets.orchestrator import PacketError, prepare_packet

        db.init_db()
        conn = db.connect()
        try:
            result = prepare_packet(conn, pid, dry_run=dry_run, force=True)
        except PacketError as e:
            conn.close()
            return HTMLResponse(f"failed: {e}", status_code=400)
        conn.close()
        return RedirectResponse(f"/packet/{result['packet_id']}", status_code=303)

    @app.post("/packet/{pid}/status")
    def set_packet_status(pid: str, status: str = Form(...)):
        conn = db.connect()
        try:
            ok = db.set_packet_status(conn, pid, status)
            if ok and status in ("applied", "withdrawn"):
                pk = db.get_packet(conn, pid)
                if pk is not None:
                    db.set_posting_status(
                        conn, pk["posting_id"],
                        "applied" if status == "applied" else "withdrawn")
        finally:
            conn.close()
        return RedirectResponse(f"/packet/{pid}", status_code=303)

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
