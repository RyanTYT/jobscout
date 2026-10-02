"""webapp/routers/inbox.py — the postings triage surface."""

from __future__ import annotations

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp import ui
from jobscout.webapp.common import (
    TEMPLATES,
    boards,
    llm_fields,
    toast,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)


def _ensure_packet(conn, posting_id: str) -> str | None:
    """Surface an interested posting on the Applications board.

    The board lists packets, so a posting only shows up there once a packet
    row exists. Marking interested creates a bare 'packet:drafting' row — no
    LLM call, no artefacts. The real packet (tailored resume, fill sheet,
    claim check) is compiled later by prepare_packet, which upserts onto the
    same deterministic id and fills in dir/model/cost.

    Existing packets are left untouched: upsert_packet's ON CONFLICT
    overwrites status, so calling this on a posting that is already filled or
    applied would silently reset it back to drafting.
    """
    if db.get_packet_for_posting(conn, posting_id) is not None:
        return None
    packet_id = f"pk-{db.sha256(posting_id)[:12]}"
    db.upsert_packet(conn, packet_id=packet_id, posting_id=posting_id,
                     status="packet:drafting")
    return packet_id


def register(app):
    @app.get("/", response_class=HTMLResponse)
    def inbox(
        request: Request,
        status: str = Query("new"),
        tier: str = Query(""),
        q: str = Query(""),
        min_score: str = Query(""),
        all_postings: str = Query(""),
        level: str = Query(""),
        location: str = Query(""),
        company: str = Query(""),
        source: str = Query(""),
        remote: str = Query(""),
        sort: str = Query("score"),
        page: str = Query("1"),
        page_size: str = Query(str(ui.DEFAULT_PAGE_SIZE)),
    ):
        filters = ui.InboxFilters.from_query(
            status=status, tier=tier, q=q, min_score=min_score,
            all_postings=all_postings, level=level, location=location,
            company=company, source=source, remote=remote, sort=sort,
        )
        conn = db.connect()
        try:
            total = db.count_postings(conn, **filters.as_kwargs())
            pg = ui.Pagination.from_query(
                total, page, page_size, filters,
            )
            rows = db.list_postings(
                conn, **filters.as_kwargs(),
                limit=pg.page_size, offset=pg.offset,
            )
            options = db.filter_options(conn)
            ctx = page_ctx("inbox", conn)
        finally:
            conn.close()

        sort_headers = ui.inbox_sort_headers(filters)

        # htmx pagination/filter requests swap only the results partial
        if request.headers.get("HX-Request") == "true":
            return TEMPLATES.TemplateResponse(
                request, "_results.html",
                {**ctx, "rows": rows, "pg": pg, "filters": filters,
                 "options": options, "sort_headers": sort_headers},
            )

        return TEMPLATES.TemplateResponse(
            request,
            "inbox.html",
            {
                **ctx,
                "rows": rows,
                "pg": pg,
                "filters": filters,
                "options": options,
                "sort_headers": sort_headers,
            },
        )

    # ── posting detail ───────────────────────────────────────────────────

    @app.get("/posting/{pid}", response_class=HTMLResponse)
    def posting_detail(request: Request, pid: str, error: str = Query("")):
        conn = db.connect()
        try:
            row = db.get_posting(conn, pid)
            packet = db.get_packet_for_posting(conn, pid) if row else None
            ctx = page_ctx("inbox", conn)
        finally:
            conn.close()
        if row is None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "posting_detail.html",
            {**ctx, "p": row, "llm": llm_fields(row), "boards": boards(row),
             "prepare_error": error,
             "packet": packet},
        )

    @app.post("/posting/{pid}/status")
    def set_status(pid: str, status: str = Form(...), request: Request = None):
        conn = db.connect()
        try:
            ok = db.set_posting_status(conn, pid, status)
            row = db.get_posting(conn, pid) if ok else None
            # interested is the hand-off point into Applications: give the
            # posting a packet row so it appears on that board, where the
            # apply launcher and the state select live.
            onboarded = _ensure_packet(conn, pid) if ok and status == "interested" \
                else None
        finally:
            conn.close()
        is_htmx = request is not None and any(
            k.lower().startswith("hx-") for k in request.headers
        )
        if is_htmx:
            if row is not None:
                resp = TEMPLATES.TemplateResponse(
                    request, "_row.html", {"request": request, "p": row}
                )
                title = (row["title"] or "")[:60]
                msg = {"interested": f"Interested — {title}",
                       "dismissed": f"Dismissed — {title}",
                       "new": f"Reset to new — {title}"}.get(status, f"{status}: {title}")
                if onboarded:
                    msg += " · added to Applications"
                tone = "danger" if status == "dismissed" else "success"
                return toast(resp, msg, tone)
            return HTMLResponse("")
        return RedirectResponse("/", status_code=303)

    # ── companies ────────────────────────────────────────────────────────

    @app.post("/posting/{pid}/prepare")
    def prepare_now(pid: str, dry_run: bool = Form(False)):
        from urllib.parse import quote as _q

        from jobscout.packets.orchestrator import PacketError, prepare_packet

        db.init_db()
        conn = db.connect()
        try:
            result = prepare_packet(conn, pid, dry_run=dry_run, force=True)
        except PacketError as e:
            conn.close()
            return RedirectResponse(
                f"/posting/{pid}?error={_q(f'packet preparation failed: {e}')}",
                status_code=303)
        conn.close()
        return RedirectResponse(
            f"/packet/{result['packet_id']}?prepared=1", status_code=303
        )
