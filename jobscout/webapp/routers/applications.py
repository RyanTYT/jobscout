"""webapp/routers/applications.py — packets, the apply launcher, run log."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)
from jobscout.webapp.common import (
    toast as _toast,
)


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def _quiet(item: dict) -> dict:
    """Days since the packet last moved (updated_at) + applied age."""
    from datetime import UTC, datetime

    pk = item["row"]
    updated = (pk["updated_at"] or "")[:10]
    days = 0
    try:
        if updated:
            days = (datetime.now(UTC).date()
                    - datetime.strptime(updated, "%Y-%m-%d").date()).days
    except ValueError:
        pass
    item["quiet_days"] = days
    item["quiet"] = days >= 14
    return item


def register(app):
    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, applied: str = Query(""),
                     auto: int = Query(0), assist: int = Query(0),
                     note: str = Query(""), error: str = Query(""),
                     q: str = Query("")):
        from jobscout.webapp.runners import apply as apply_mod

        conn = db.connect()
        try:
            apply_mod.refresh_runs(conn)          # drain sidecar events → statuses
            packets = db.list_packets(conn)
            email_events = db.recent_email_events(conn, 30)
            event_hits = (db.search_app_events(conn, q)
                          if q.strip() else [])
            ctx = page_ctx("applications", conn)
            runs = db.list_apply_runs(conn)
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
            manifests.append(_quiet({"row": pk, "manifest": manifest,
                              "missing": apply_mod.sheet_missing_count(pk)}))
        return TEMPLATES.TemplateResponse(
            request, "applications.html",
            {**ctx, "packets": manifests, "runs": runs,
             "email_events": email_events, "event_hits": event_hits,
             "q": q,
             "runs_active": any(r["status"] in ("launched", "running")
                                for r in runs),
             "just_applied": applied == "1", "error": error, "auto_count": auto,
             "assist_count": assist, "apply_note": note, "apply_error": error},
        )

    @app.post("/packet/{pid}/events")
    async def packet_add_event(pid: str, request: Request):
        """Manual timeline entry: OA questions, interview notes, anything."""
        from urllib.parse import quote as _q

        form = await request.form()
        kind = form.get("kind", "note")
        event_date = (form.get("event_date", "") or "").strip()
        title = (form.get("title", "") or "").strip()
        notes = (form.get("notes", "") or "").strip()
        if kind not in db.EVENT_KINDS:
            return RedirectResponse(
                f"/packet/{pid}?error={_q('unknown event kind')}",
                status_code=303)
        if not title and not notes:
            msg = _q("give the entry a title or some notes")
            return RedirectResponse(
                f"/packet/{pid}?error={msg}", status_code=303)
        conn = db.connect()
        try:
            if db.get_packet(conn, pid) is None:
                return RedirectResponse("/applications", status_code=303)
            db.record_app_event(conn, packet_id=pid, kind=kind,
                                event_date=event_date, title=title,
                                notes=notes, auto=True)
        finally:
            conn.close()
        return RedirectResponse(f"/packet/{pid}", status_code=303)

    @app.post("/packet/{pid}/events/{eid}/delete")
    def packet_delete_event(pid: str, eid: int):
        conn = db.connect()
        try:
            db.delete_app_event(conn, eid)
        finally:
            conn.close()
        return RedirectResponse(f"/packet/{pid}", status_code=303)

    @app.post("/packet/{pid}/follow-up")
    def packet_follow_up(pid: str):
        """The nudge: draft a follow-up email (background; draft-first —
        appended to the mail account's Drafts when linked, never sent)."""
        from jobscout.webapp.runners import outreach as outreach_runner

        conn = db.connect()
        try:
            pk = db.get_packet(conn, pid)
        finally:
            conn.close()
        if pk is None:
            return RedirectResponse("/applications", status_code=303)
        outreach_runner.start_follow_up(pid)
        return RedirectResponse(f"/packet/{pid}?nudge=1", status_code=303)

    @app.get("/packet/{pid}/follow-up-status", response_class=HTMLResponse)
    def packet_follow_up_status(request: Request, pid: str):
        """HTMX partial: the follow-up draft state (polled while running)."""
        from jobscout.webapp.runners import outreach as outreach_runner

        conn = db.connect()
        try:
            pk = db.get_packet(conn, pid)
            company_id = None
            if pk is not None and "company_id" in pk.keys():
                company_id = pk["company_id"]
            row = (db.latest_outreach(conn, company_id, "follow_up")
                   if company_id else None)
        finally:
            conn.close()
        running = outreach_runner.follow_up_running(pid)
        return TEMPLATES.TemplateResponse(
            request, "_followup.html",
            {"pid": pid, "running": running, "row": row},
        )

    @app.post("/applications/email-check")
    def applications_email_check():
        """Manual 'check now' for the email tracker (background thread)."""
        from jobscout.webapp.runners import email_tracker

        if not email_tracker.enabled():
            return RedirectResponse(
                "/applications?error=email+tracking+is+off+—+enable+it+"
                "on+the+Ops+page", status_code=303)
        email_tracker.check_now()
        return RedirectResponse("/applications", status_code=303)

    @app.get("/applications/runs", response_class=HTMLResponse)
    def applications_runs(request: Request):
        """HTMX partial: live apply-run statuses (drains sidecar events)."""
        from jobscout.webapp.runners import apply as apply_mod

        conn = db.connect()
        try:
            apply_mod.refresh_runs(conn)
            runs = db.list_apply_runs(conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request, "_runs.html",
            {"runs": runs,
             "runs_active": any(r["status"] in ("launched", "running")
                                for r in runs)},
        )

    @app.post("/applications/apply")
    async def applications_apply(request: Request):
        """Launch the selected packets (checkboxes on the packet board)."""
        from jobscout.webapp.runners import apply as apply_mod

        form = await request.form()
        selected = form.getlist("pk")
        conn = db.connect()
        try:
            try:
                result = apply_mod.launch_apply(conn, selected)
                note = " ".join(result.get("note") or [])
                return RedirectResponse(
                    "/applications?applied=1"
                    + (f"&auto={len(result['automated'])}" if result["automated"] else "")
                    + (f"&assist={len(result['assisted'])}" if result["assisted"] else "")
                    + (f"&note={quote(note)}" if note else ""),
                    status_code=303,
                )
            except apply_mod.ApplyError as e:
                return RedirectResponse(
                    f"/applications?error={quote(str(e))}", status_code=303)
        finally:
            conn.close()

    @app.get("/packet/{pid}", response_class=HTMLResponse)
    def packet_detail(request: Request, pid: str, prepared: str = Query(""),
                      nudge: str = Query("")):
        conn = db.connect()
        try:
            pk = db.get_packet(conn, pid)
            posting = db.get_posting(conn, pk["posting_id"]) if pk else None
            ctx = page_ctx("applications", conn)
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
                **ctx,
                "pk": pk,
                "posting": posting,
                "manifest": _load("packet.yaml") or {},
                "fill_sheet": (_load("fill_sheet.yaml") or {}).get("fields", []),
                "claims": _load("claim_check.yaml") or [],
                "resume_md": _load("resume.md"),
                "cover_letter": _load("cover_letter.md"),
                "tailor": _load("tailor.yaml") or {},
                "just_prepared": prepared == "1",
                "nudge": nudge == "1",
                "today": _today(),
            },
        )

    @app.post("/packet/{pid}/status")
    def set_packet_status(pid: str, status: str = Form(...),
                          request: Request = None):
        """Manual follow-through: update the application state (applied →
        interviewing → offer/rejected...). HTMX swaps the row in place; a
        plain post redirects back to the packet page."""
        conn = db.connect()
        try:
            ok = db.set_packet_status(conn, pid, status)
            if ok and status in ("applied", "withdrawn"):
                pk = db.get_packet(conn, pid)
                if pk is not None:
                    db.set_posting_status(
                        conn, pk["posting_id"],
                        "applied" if status == "applied" else "withdrawn")
            row = db.get_packet(conn, pid) if ok else None
        finally:
            conn.close()
        is_htmx = request is not None and any(
            k.lower().startswith("hx-") for k in request.headers)
        if is_htmx and row is not None:
            manifest = {}
            if row["dir"]:
                mp = Path(row["dir"]) / "packet.yaml"
                if mp.is_file():
                    try:
                        import yaml as _yaml

                        manifest = _yaml.safe_load(
                            mp.read_text(encoding="utf-8")) or {}
                    except ValueError:
                        manifest = {}
            from jobscout.webapp.runners.apply import sheet_missing_count

            missing = sheet_missing_count(row)
            item = _quiet({"row": row, "manifest": manifest,
                           "missing": missing})
            resp = TEMPLATES.TemplateResponse(
                request, "_packet_row.html",
                {"request": request, "pk": row, "man": manifest,
                 "missing": missing, "quiet_days": item["quiet_days"],
                 "quiet": item["quiet"]})
            return _toast(resp, f"State updated: {status}", "success")
        return RedirectResponse(f"/packet/{pid}", status_code=303)

    # ── ops ──────────────────────────────────────────────────────────────
