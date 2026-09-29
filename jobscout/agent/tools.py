"""Agent tools (PLAN §1.2). Safe by construction:

  * web_search / fetch — read-only network
  * db_query — SELECT-only, table allowlist, forced LIMIT
  * add_company / add_signal / write_note — state changes go through the
    same core functions the CLI uses (discovery.add_candidate,
    db.upsert_signal); write_note is path-sanitized into research/

Every tool returns a string (the tool result the model sees).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from jobscout.core import db
from jobscout.core import paths as core_paths
from jobscout.core.schema import ProfileCfg, Settings
from jobscout.sources.ats.base import soft_get, strip_html

SYSTEM_PROMPT = (
    "You are jobscout's discovery agent. You find target companies for a senior "
    "quant/backend engineer: firms that might not advertise on job boards. "
    "You act ONLY through the provided tools. Be decisive and frugal with steps: "
    "each search and fetch costs budget. When you finish, produce a concise "
    "final summary. Never invent domains or facts — if a tool errors, say so "
    "in your summary."
)

_ALLOWED_TABLES = {"companies", "postings", "signals", "packets", "runs", "llm_cache", "llm_calls", "state"}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "get_context",
            "description": "Load current state: watchlist tiers, dark-pool companies, recent signals, unresolved mentions, yesterday's digest. Call this FIRST.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Web search. Use for company discovery: funding news, 'we are hiring' posts, new ATS boards. Returns title/url/snippet per result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "description": "Max results (default 5, max 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": "Fetch a web page and return its readable text (~3000 chars). Use to read careers pages / articles found via search.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_query",
            "description": "Read-only SQL SELECT against the jobscout database (tables: companies, postings, signals, packets, runs, llm_cache, llm_calls, state). LIMIT is enforced.",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_company",
            "description": "Add a discovered company to the watchlist (tier 'candidate', ATS boards probed automatically). Requires a real domain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "domain": {"type": "string", "description": "e.g. acme.com — must come from evidence, never invented"},
                    "note": {"type": "string", "description": "One-line reason + source"},
                },
                "required": ["name", "domain", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_signal",
            "description": "Record an observation about a company (funding, blog post, hiring mention). Deduped by note content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string"},
                    "kind": {"type": "string", "description": "funding | hn | blog | search | agent"},
                    "note": {"type": "string"},
                },
                "required": ["company_name", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "Write a markdown research note into research/ (e.g. a dark-pool deep-dive with sources).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Simple name, e.g. 'twosigma-hiring-signals'"},
                    "content": {"type": "string", "description": "Markdown content"},
                },
                "required": ["filename", "content"],
            },
        },
    },
]


@dataclass
class AgentCtx:
    conn: sqlite3.Connection
    wl: object
    profile: ProfileCfg
    settings: Settings
    env: dict
    client: httpx.Client
    events: list = field(default_factory=list)


# ── implementations ──────────────────────────────────────────────────────────


def _get_context(ctx: AgentCtx) -> str:
    conn = ctx.conn
    parts: list[str] = []
    tiers = conn.execute(
        "SELECT tier, COUNT(*) AS n FROM companies GROUP BY tier ORDER BY tier"
    ).fetchall()
    parts.append("watchlist: " + ", ".join(f"{r['tier']}={r['n']}" for r in tiers))
    candidates = conn.execute(
        "SELECT name, domain FROM companies WHERE tier = 'candidate' ORDER BY name LIMIT 15"
    ).fetchall()
    if candidates:
        parts.append("candidates: " + "; ".join(f"{r['name']} ({r['domain']})" for r in candidates))
    dark = conn.execute(
        "SELECT name, domain FROM companies WHERE non_ats = 1 ORDER BY name LIMIT 15"
    ).fetchall()
    if dark:
        parts.append("dark-pool (no public ATS board): " + "; ".join(
            f"{r['name']} ({r['domain']})" for r in dark))
    recent = conn.execute(
        "SELECT s.note, c.name FROM signals s LEFT JOIN companies c "
        "ON s.company_id = c.id ORDER BY s.seen_at DESC LIMIT 10"
    ).fetchall()
    if recent:
        parts.append("recent signals:\n" + "\n".join(
            f"  - {(r['name'] or 'unresolved')}: {(r['note'] or '')[:100]}" for r in recent))
    unresolved = conn.execute(
        "SELECT note FROM signals WHERE company_id IS NULL AND kind = 'funding' LIMIT 8"
    ).fetchall()
    if unresolved:
        parts.append("unresolved funding mentions (find their domains): " + "; ".join(
            (r["note"] or "")[:80] for r in unresolved))
    digest = core_paths.digest_dir()
    files = sorted(digest.glob("*.md"))
    if files:
        tail = files[-1].read_text(encoding="utf-8").splitlines()
        parts.append("latest digest tail:\n" + "\n".join(tail[-25:]))
    return "\n\n".join(parts)[:6000]


def _fmt(items: list[tuple[str, str, str]]) -> str:
    if not items:
        return "no results"
    return "\n".join(
        f"{i+1}. {t} — {u}\n   {s[:160]}" for i, (t, u, s) in enumerate(items))


def _cse_search(query: str, n: int, ctx: AgentCtx) -> str | None:
    key = (ctx.env.get("JOBSCOUT_CSE_API_KEY") or "").strip()
    cx = (ctx.env.get("JOBSCOUT_CSE_CX") or "").strip()
    if not (key and cx):
        return None
    r = soft_get(ctx.client, "https://www.googleapis.com/customsearch/v1",
                 params={"key": key, "cx": cx, "q": query, "num": n})
    if r is None:
        return None
    items = r.json().get("items") or []
    return _fmt([(it.get("title", ""), it.get("link", ""),
                  it.get("snippet", "")) for it in items[:n]])


def _brave_search(query: str, n: int, ctx: AgentCtx) -> str | None:
    brave = (ctx.env.get("JOBSCOUT_BRAVE_API_KEY") or "").strip()
    if not brave:
        return None
    r = _brave_get(ctx, query, n)
    if r is None:
        return None
    results = r.json().get("web", {}).get("results") or []
    return _fmt([(it.get("title", ""), it.get("url", ""), "")
                 for it in results[:n]])


def _llm_search(query: str, n: int, ctx: AgentCtx) -> str | None:
    """Search-grounded chat model as the engine (no extra key — reuses the
    LLM provider; e.g. OpenRouter :online plugins or sonar-style models).
    Prompts for a JSON result list and parses it; a few cents per run."""
    model = ((ctx.settings.search.llm_model or "").strip()
             if ctx.settings else "")
    if not model:
        return None
    import json as _json

    settings = ctx.settings or None
    base = (ctx.env.get((settings.llm.base_url_env if settings
                         else "JOBSCOUT_LLM_BASE_URL") or "")
            or "").strip().rstrip("/")
    key = (ctx.env.get((settings.llm.api_key_env if settings
                        else "JOBSCOUT_LLM_API_KEY") or "")
           or "").strip()
    if not (base and key):
        return None
    prompt = (
        f"Search the web for: {query}\n"
        f"Return ONLY a JSON array of the top {n} results, each exactly "
        '"[{\"title\": \"...\", \"url\": \"...\", \"snippet\": \"...\"}]". '
        "No prose, no markdown fences."
    )
    try:
        r = ctx.client.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0},
            timeout=30.0,
        )
        r.raise_for_status()
        text = (r.json().get("choices") or [{}])[0].get("message", {}).get(
            "content", "")
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return None
        items = _json.loads(text[start:end + 1])
        return _fmt([(str(i.get("title", "")), str(i.get("url", "")),
                      str(i.get("snippet", ""))) for i in items[:n]])
    except Exception:
        return None


def _ddg_search(query: str, n: int, ctx: AgentCtx) -> str | None:
    """DuckDuckGo via the ddgs package — free, no key. Unofficial endpoint:
    rate-limited and occasionally broken; fine as a manual fallback."""
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=n))
        return _fmt([(r.get("title", ""), r.get("href", r.get("url", "")),
                      r.get("body", "")) for r in results[:n]])
    except Exception:
        return None


# engines resolve at CALL time (globals lookup) so tests can patch them
SEARCH_ENGINE_NAMES = ("cse", "brave", "llm", "ddg")


def _engine(name: str):
    return globals().get(f"_{name}_search")


def _web_search(query: str, num_results: int = 5, ctx: AgentCtx = None) -> str:
    n = max(1, min(8, int(num_results or 5)))
    cfg = (ctx.settings.search if ctx and ctx.settings else None)
    chosen = (cfg.provider if cfg else "auto")

    if chosen == "auto":
        order = ["cse", "brave", "llm", "ddg"]
    else:
        order = [chosen]
    for name in order:
        fn = _engine(name)
        result = fn(query, n, ctx) if fn else None
        if result is not None:
            return result
    return (f"no search provider produced results (provider={chosen}). "
            "Configure CSE/Brave keys, a search model slug, or pick ddg "
            "on the Ops page — meanwhile use fetch() on known URLs.")


def _brave_get(ctx: AgentCtx, query: str, n: int):
    headers = {"X-Subscription-Token": (ctx.env.get("JOBSCOUT_BRAVE_API_KEY") or "").strip(),
               "Accept": "application/json"}
    try:
        return ctx.client.get("https://api.search.brave.com/res/v1/web/search",
                              params={"q": query, "count": n}, headers=headers)
    except httpx.HTTPError:
        return None


def _fetch(url: str, ctx: AgentCtx) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "invalid url"
    r = soft_get(ctx.client, url)
    if r is None:
        return f"fetch failed (non-200 or network error): {url}"
    title_m = _TITLE_RE.search(r.text)
    title = strip_html(title_m.group(1)) if title_m else ""
    text = strip_html(r.text)[:3000]
    return (f"{title}\n\n{text}")[:3200]


def _db_query(sql: str, ctx: AgentCtx) -> str:
    s = (sql or "").strip()
    low = s.lower()
    if not low.startswith("select"):
        return "rejected: only SELECT queries are allowed"
    if s.count(";") > 1 or (s.endswith(";") and s.count(";") > 1):
        return "rejected: single statement only"
    s = s.rstrip(";")
    for word in ("pragma", "attach", "insert", "update", "delete", "drop", "alter"):
        if re.search(rf"\b{word}\b", low):
            return f"rejected: {word} not allowed"
    tables = set(re.findall(r"\bfrom\s+([a-z_]+)", low))
    bad = tables - _ALLOWED_TABLES
    if tables and bad:
        return f"rejected: tables not allowed: {', '.join(sorted(bad))}"
    if "limit" not in low:
        s += " LIMIT 100"
    try:
        rows = ctx.conn.execute(s).fetchall()
    except sqlite3.Error as e:
        return f"sql error: {e}"
    if not rows:
        return "0 rows"
    cols = rows[0].keys()
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:40]:
        lines.append("| " + " | ".join(str(r[c])[:40] if r[c] is not None else "" for c in cols) + " |")
    return f"{len(rows)} rows (showing up to 40)\n" + "\n".join(lines)


def _add_company(name: str, domain: str, note: str, ctx: AgentCtx) -> str:
    from jobscout.sources.discovery import add_candidate

    domain = (domain or "").lower().strip().removeprefix("https://").removeprefix("http://")
    domain = domain.split("/")[0].removeprefix("www.").rstrip(".")
    if "." not in domain:
        return f"rejected: {domain!r} is not a domain — never invent one"
    added, tokens = add_candidate(
        ctx.conn, ctx.wl, name, domain, "agent-morning",
        note or "discovered by agent", ctx.client,
    )
    if added:
        toks = ", ".join(f"{p}:{t}" for p, t in tokens.items()) or "none (dark-pool entry)"
        return f"added {name} ({domain}) to watchlist candidates; ATS boards: {toks}"
    return f"{name} ({domain}) already on the watchlist — not added"


def _add_signal(company_name: str, kind: str, note: str, ctx: AgentCtx) -> str:
    from jobscout import watchlist as wlmod

    found = wlmod.find(ctx.wl, company_name or "")
    company_id = found[1] and db.slugify(found[1].name) if found else None
    cid = company_id or None
    key = db.sha256(f"agent|{cid or (company_name or '')}|{(note or '')[:120]}")
    is_new = db.upsert_signal(ctx.conn, kind=kind or "agent", key=key,
                              company_id=cid, note=f"{company_name}: {note}" if company_name else note)
    label = found[1].name if found else company_name
    return (f"signal recorded on {label}" if is_new else f"duplicate signal skipped ({label})")


def _write_note(filename: str, content: str, ctx: AgentCtx) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", (filename or "").strip())
    safe = safe.strip(".-")
    if not safe:
        return "rejected: bad filename"
    if not safe.endswith(".md"):
        safe += ".md"
    if not safe.startswith("agent-"):
        safe = f"agent-{safe}"
    out = core_paths.research_dir() / safe
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content or "", encoding="utf-8")
    return f"note written: {out}"


_IMPLEMENTATIONS = {
    "get_context": _get_context,
    "web_search": _web_search,
    "fetch": _fetch,
    "db_query": _db_query,
    "add_company": _add_company,
    "add_signal": _add_signal,
    "write_note": _write_note,
}


def execute(name: str, args: dict, ctx: AgentCtx) -> str:
    """Run one tool. Tool failures are returned as strings, never raised."""
    fn = _IMPLEMENTATIONS.get(name)
    if fn is None:
        return f"unknown tool: {name}"
    try:
        if name == "get_context":
            return fn(ctx)
        return fn(**(args or {}), ctx=ctx)
    except TypeError as e:
        return f"tool error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 — tool failures are results, not crashes
        return f"tool error: {e}"
