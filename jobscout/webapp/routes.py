"""Dashboard routes (PLAN §7). Thin controllers: parse → query → render.

Heavy lifting lives in webapp/ui.py (filter state, pagination) and
core/db.py (queries). Templates compose from templates/components/macros.html
and styles come exclusively from static/css/tokens.css variables.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobscout import __version__
from jobscout.core import db
from jobscout.webapp import ui

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.globals["linkify"] = ui.linkify
TEMPLATES.env.globals["kind_meta"] = ui.signal_kind_meta
TEMPLATES.env.globals["kind_desc"] = lambda k: ui.signal_kind_meta(k)["desc"]

_run_state: dict = {"running": False, "out": "", "error": ""}


def _strip_ansi(text: str) -> str:
    """Console colour codes have no business in a web page."""
    import re as _re

    return _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")


def _last_error(text: str) -> str:
    """The last `SomethingError: message` line from a CLI traceback."""
    import re as _re

    hits = _re.findall(
        r"[A-Za-z_]*(?:Error|Exception|ConfigError)\b[^\n]*",
        text or "")
    return hits[-1].strip() if hits else ""


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


def _toast(response, message: str, tone: str = "success"):
    """Server-driven toast: app.js surfaces X-Toast on htmx responses."""
    response.headers["X-Toast"] = quote(message)
    response.headers["X-Toast-Tone"] = tone
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="jobscout", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    db.init_db()  # idempotent; also migrates schema

    def _ctx(active: str, conn) -> dict:
        counts = ui.nav_counts(conn)
        try:
            from jobscout.webapp import profile_store
            counts["profile_missing"] = len(profile_store.missing_required())
        except Exception:  # noqa: BLE001 — resume unreadable must not kill nav
            counts["profile_missing"] = 0
        return ui.template_ctx(__version__, active, counts)

    # ── inbox ────────────────────────────────────────────────────────────

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
            ctx = _ctx("inbox", conn)
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
            ctx = _ctx("inbox", conn)
        finally:
            conn.close()
        if row is None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "posting_detail.html",
            {**ctx, "p": row, "llm": _llm_fields(row), "boards": _boards(row),
             "prepare_error": error,
             "packet": packet},
        )

    @app.post("/posting/{pid}/status")
    def set_status(pid: str, status: str = Form(...), request: Request = None):
        conn = db.connect()
        try:
            ok = db.set_posting_status(conn, pid, status)
            row = db.get_posting(conn, pid) if ok else None
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
                tone = "danger" if status == "dismissed" else "success"
                return _toast(resp, msg, tone)
            return HTMLResponse("")
        return RedirectResponse("/", status_code=303)

    # ── companies ────────────────────────────────────────────────────────

    @app.get("/companies/{cid}", response_class=HTMLResponse)
    def company_edit(request: Request, cid: str, error: str = Query("")):
        from urllib.parse import quote as _q

        from jobscout.webapp import watchlist_store

        conn = db.connect()
        try:
            ctx = _ctx("companies", conn)
        finally:
            conn.close()
        try:
            tier, entry = watchlist_store.find(cid)
        except watchlist_store.WatchlistStoreError as e:
            return RedirectResponse(
                f"/companies?error={_q(str(e))}", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "company_detail.html",
            {**ctx, "cid": cid, "tier": tier, "entry": entry,
             "tiers": watchlist_store.TIERS, "error": error},
        )

    @app.post("/companies/{cid}/save")
    async def company_save(request: Request, cid: str):
        from urllib.parse import quote as _q

        from jobscout.webapp import watchlist_store

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
            ctx = _ctx("companies", conn)
        finally:
            conn.close()
        boards = {r["id"]: _boards(r) for r in rows}
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
            {**ctx, "rows": rows, "boards": boards, "signals": signals,
             "kinds": kinds, "company_saved": company_saved == "1",
             "error": error, "found_via": found_via},
        )

    # ── discovery ────────────────────────────────────────────────────────

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
            ctx = _ctx("discovery", conn)
        finally:
            conn.close()
        from jobscout.core.config import load_profile

        try:
            target = load_profile().target
        except Exception:                 # noqa: BLE001 — profile optional here
            from jobscout.core.models import TargetCfg

            target = TargetCfg()
        reports = sorted(BASE_DIR.parents[1].joinpath("morning_reports").glob("*.md"))
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
                "running": _run_state["running"],
                "run_out": _run_state["out"],
                "run_error": _run_state["error"],
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
    def run_agent_now():
        """Run the morning agent in a background thread — the button
        returns immediately; the output panel below polls until it lands.
        The CLI resolution works both in dev (.venv/bin/jobscout) and in
        the packaged app (sys.executable IS the frozen jobscout-server)."""
        import subprocess
        import sys
        import threading

        if _run_state["running"]:
            return RedirectResponse("/discovery?started=1", status_code=303)
        _run_state["running"] = True
        _run_state["out"] = ""

        def _launch():
            try:
                if getattr(sys, "frozen", False):
                    cli = sys.executable            # the frozen binary itself
                else:
                    cli = str(Path(sys.executable).parent / "jobscout")
                proc = subprocess.run(
                    [cli, "agent"], capture_output=True, text=True, timeout=900
                )
                _run_state["out"] = _strip_ansi(
                    (proc.stdout or "") + (proc.stderr or ""))[-8000:]
                if proc.returncode != 0:
                    _run_state["error"] = _last_error(_run_state["out"]) or (
                        f"the agent exited with code {proc.returncode}")
                else:
                    _run_state["error"] = ""
            except subprocess.TimeoutExpired:
                _run_state["out"] = ("run timed out after 900s "
                                     "(caps should prevent this)")
                _run_state["error"] = "run timed out after 900s"
            except OSError as e:
                _run_state["out"] = f"failed to launch the jobscout agent: {e}"
                _run_state["error"] = _run_state["out"]
            finally:
                _run_state["running"] = False

        threading.Thread(target=_launch, daemon=True).start()
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
        return TEMPLATES.TemplateResponse(
            request, "_run_status.html",
            {"running": _run_state["running"], "run_out": _run_state["out"],
             "run_error": _run_state["error"], "agent_spend": agent_spend},
        )

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
            {"spend": spend, "running": _run_state["running"]},
        )

    # ── applications / packets ───────────────────────────────────────────

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, applied: str = Query(""),
                     auto: int = Query(0), assist: int = Query(0),
                     note: str = Query(""), error: str = Query("")):
        from jobscout.webapp import apply as apply_mod

        conn = db.connect()
        try:
            apply_mod.refresh_runs(conn)          # drain sidecar events → statuses
            packets = db.list_packets(conn)
            ctx = _ctx("applications", conn)
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
            manifests.append({"row": pk, "manifest": manifest,
                              "missing": apply_mod.sheet_missing_count(pk)})
        return TEMPLATES.TemplateResponse(
            request, "applications.html",
            {**ctx, "packets": manifests, "runs": runs,
             "runs_active": any(r["status"] in ("launched", "running")
                                for r in runs),
             "just_applied": applied == "1", "auto_count": auto,
             "assist_count": assist, "apply_note": note, "apply_error": error},
        )

    @app.get("/applications/runs", response_class=HTMLResponse)
    def applications_runs(request: Request):
        """HTMX partial: live apply-run statuses (drains sidecar events)."""
        from jobscout.webapp import apply as apply_mod

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
        from jobscout.webapp import apply as apply_mod

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

    @app.post("/ops/models")
    async def ops_models_save(request: Request):
        """models.yaml editor: tier models + prices + caps, with rollback."""
        from urllib.parse import quote as _q

        from jobscout.webapp import models_store

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

        from jobscout.webapp import models_store

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

        from jobscout.webapp import key_store

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
        from jobscout.webapp import key_store

        key_store.clear_cse()
        return RedirectResponse("/ops?key_saved=1", status_code=303)

    @app.post("/ops/llm-key")
    async def ops_llm_key(request: Request):
        """Save the LLM API key into .env (gitignored; live without restart).
        The key itself is never rendered back — only a masked tail."""
        from urllib.parse import quote as _q

        from jobscout.webapp import key_store

        form = await request.form()
        try:
            key_store.save_key(form.get("api_key", ""))
            return RedirectResponse("/ops?key_saved=1", status_code=303)
        except key_store.KeyStoreError as e:
            return RedirectResponse(
                f"/ops?key_error={_q(str(e))}", status_code=303)

    @app.post("/ops/llm-key/clear")
    async def ops_llm_key_clear(request: Request):
        from jobscout.webapp import key_store

        key_store.clear_key()
        return RedirectResponse("/ops?key_saved=1", status_code=303)

    @app.get("/packet/{pid}", response_class=HTMLResponse)
    def packet_detail(request: Request, pid: str, prepared: str = Query("")):
        conn = db.connect()
        try:
            pk = db.get_packet(conn, pid)
            posting = db.get_posting(conn, pk["posting_id"]) if pk else None
            ctx = _ctx("applications", conn)
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
            },
        )

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

    # ── ops ──────────────────────────────────────────────────────────────

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request, key_saved: str = Query(""),
            key_error: str = Query(""), models_saved: str = Query(""),
            models_error: str = Query(""),
            models_refreshed: str = Query("")):
        from jobscout.webapp import key_store, models_store

        try:
            cse_status = key_store.cse_status()
        except Exception:                       # noqa: BLE001
            cse_status = {"key_set": False, "key_tail": "", "cx": ""}
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
            ctx = _ctx("ops", conn)
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
                "models_cfg": models_cfg,
                "models_saved": models_saved == "1",
                "models_error": models_error,
                "models_refreshed": models_refreshed,
                "running": _run_state["running"],
                "key_saved": key_saved == "1",
                "key_error": key_error,},
        )

    # ── profile (master resume editor) ───────────────────────────────────

    @app.get("/profile/resume-download")
    def profile_resume_download():
        """Serve the current master resume as a download (fill it fully
        offline, re-upload via the form above)."""
        from starlette.responses import FileResponse

        from jobscout.core.paths import master_resume_dir

        path = master_resume_dir() / "resume.yaml"
        if not path.is_file():
            return RedirectResponse("/profile?upload_error=resume.yaml+not+found",
                                    status_code=303)
        return FileResponse(path, filename="resume.yaml",
                            media_type="application/x-yaml")

    @app.post("/profile/upload-resume")
    async def profile_upload_resume(request: Request):
        """Replace the master resume wholesale from an uploaded YAML file.
        Validated against the schema first; the old file is kept as .bak."""
        from urllib.parse import quote as _q

        from jobscout.webapp import profile_store

        form = await request.form()
        upload = form.get("resume_file")
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse(
                "/profile?upload_error=" + _q("no file selected"),
                status_code=303)
        name = upload.filename.lower()
        if not name.endswith((".yaml", ".yml")):
            return RedirectResponse(
                "/profile?upload_error=" + _q("expected a .yaml file"),
                status_code=303)
        try:
            text = (await upload.read()).decode("utf-8")
        except UnicodeDecodeError:
            return RedirectResponse(
                "/profile?upload_error=" + _q("not a UTF-8 text file"),
                status_code=303)
        try:
            result = profile_store.upload_resume(text)
            return RedirectResponse(
                "/profile?uploaded=1&fields=" + _q(str(result["fields"])),
                status_code=303)
        except profile_store.ProfileError as e:
            return RedirectResponse(
                "/profile?upload_error=" + _q(str(e)), status_code=303)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = Query(""),
                     uploaded: str = Query(""), upload_error: str = Query(""),
                     fields: str = Query("")):
        from jobscout.webapp import profile_store

        conn = db.connect()
        try:
            ctx = _ctx("profile", conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request, "profile.html",
            {
                **ctx,
                "fields": profile_store.FIELDS,
                "field_options": {
                    f.key: [(o, o, None) for o in f.options]
                    for f in profile_store.FIELDS
                },
                "values": profile_store.current_values(),
                "missing": profile_store.missing_required(),
                "custom": profile_store.custom_fields(),
                "just_saved": saved == "1",
                "just_uploaded": uploaded == "1",
                "upload_error": upload_error,
                "uploaded_fields": fields,
                "error": None,
            },
        )

    @app.post("/profile/save")
    async def profile_save(request: Request):
        from jobscout.webapp import profile_store

        form = dict(await request.form())
        try:
            profile_store.save_profile(form)
            return RedirectResponse("/profile?saved=1", status_code=303)
        except profile_store.ProfileError as e:
            conn = db.connect()
            try:
                ctx = _ctx("profile", conn)
            finally:
                conn.close()
            return TEMPLATES.TemplateResponse(
                request, "profile.html",
                {
                    **ctx,
                    "fields": profile_store.FIELDS,
                    "field_options": {
                        f.key: [(o, o, None) for o in f.options]
                        for f in profile_store.FIELDS
                    },
                    "values": profile_store.current_values(),
                    "missing": profile_store.missing_required(),
                    "custom": profile_store.custom_fields(),
                    "just_saved": False,
                    "error": str(e),
                },
                status_code=422,
            )

    return app
