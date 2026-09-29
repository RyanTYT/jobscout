"""webapp/common.py — shared route plumbing (the router modules' imports).

One TEMPLATES instance with the template globals, the nav-count context
builder, and the row/toast helpers every page uses. Routers stay thin:
they import from here and register their routes on the app.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

from jobscout import __version__
from jobscout.webapp import ui
from jobscout.webapp.runners import agent_runner

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.globals["linkify"] = ui.linkify
TEMPLATES.env.globals["kind_meta"] = ui.signal_kind_meta
TEMPLATES.env.globals["kind_desc"] = lambda k: ui.signal_kind_meta(k)["desc"]


def _from_json(value):
    """Parse a stored JSON string for templates (outreach drafts)."""
    try:
        return json.loads(value) if value else None
    except (ValueError, TypeError):
        return None


TEMPLATES.env.filters["from_json"] = _from_json


def ctx(active: str, conn) -> dict:
    """Nav context: counts + the profile-missing badge."""
    counts = ui.nav_counts(conn)
    try:
        from jobscout.webapp.stores import profile_store
        counts["profile_missing"] = len(profile_store.missing_required())
    except Exception:  # noqa: BLE001 — resume unreadable must not kill nav
        counts["profile_missing"] = 0
    return ui.template_ctx(__version__, active, counts)


def llm_fields(row) -> dict:
    raw = row["llm_json"] if "llm_json" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def boards(row) -> list[tuple[str, str]]:
    raw = row["ats_tokens"] if "ats_tokens" in row.keys() else None
    if not raw:
        return []
    try:
        return list(json.loads(raw).items())
    except ValueError:
        return {}


def toast(response, message: str, tone: str = "success"):
    """Server-driven toast: app.js surfaces X-Toast on htmx responses."""
    response.headers["X-Toast"] = quote(message)
    response.headers["X-Toast-Tone"] = tone
    return response


def llm_ctx(conn):
    """The agent-spend + run-state pair the ops/discovery panels share."""
    return agent_runner.agent_spend(conn), agent_runner.state()
