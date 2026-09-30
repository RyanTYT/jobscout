"""webapp/runners/outreach.py — cold-email + LinkedIn reachout generation.

For companies where nothing matches your profile (the email/monitor
route), this drafts the way in:

  cold_email — a concise, resume-grounded interest email (subject +
  body), ready to send to the company's contact inbox (mailto link in
  the UI).

  linkedin — searches the live web for LinkedIn profiles at the company
  (recruiters / talent / engineering managers, via the SAME search
  engine chain the agent uses), then drafts a connection note (≤300
  chars) + follow-up message per contact.

Both run in background threads (the UI polls a partial); results are
stored in the outreach table (one draft per company+kind, regenerable)
and every LLM call is metered in llm_calls via LlmClient (quality tier).
Grounding rule: the drafts may only state facts from the master resume
and the provided company context — same honesty contract as packets.
"""

from __future__ import annotations

import json
import re
import threading

from jobscout.core import db

_state: dict[str, dict] = {}

KINDS = ("cold_email", "linkedin", "follow_up")


# ── lifecycle ───────────────────────────────────────────────────────────────


def running(company_id: str, kind: str) -> bool:
    return _state.get(f"{company_id}:{kind}", {}).get("running", False)


def start(company_id: str, kind: str) -> None:
    if kind not in KINDS:
        return
    key = f"{company_id}:{kind}"
    if _state.get(key, {}).get("running"):
        return
    _state[key] = {"running": True, "error": ""}
    threading.Thread(target=_run, args=(company_id, kind),
                     daemon=True).start()


def _run(company_id: str, kind: str) -> None:
    key = f"{company_id}:{kind}"
    conn = None
    try:
        conn = db.connect()
        _generate(conn, company_id, kind)
    except Exception as e:                     # noqa: BLE001 — surfaced in UI
        _state[key] = {"running": False, "error": str(e)[:300]}
        if conn is not None:
            db.upsert_outreach(conn, company_id=company_id, kind=kind,
                               status="failed", error=str(e)[:300])
    finally:
        st = _state.get(key, {})
        _state[key] = {"running": False, "error": st.get("error", "")}
        if conn is not None:
            conn.close()


def status(conn, company_id: str, kind: str) -> dict:
    """Merged view for the UI: runner state + the stored draft."""
    out = {"running": running(company_id, kind), "error": "", "content": None,
           "status": None, "cost_usd": 0.0}
    row = db.latest_outreach(conn, company_id, kind)
    if row is not None:
        out["status"] = row["status"]
        out["content"] = row["content"]
        out["cost_usd"] = row["cost_usd"] or 0.0
        if row["error"]:
            out["error"] = row["error"]
    if out["running"]:
        out["error"] = ""
    return out


# ── context ─────────────────────────────────────────────────────────────────


def _company_context(conn, company_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"unknown company: {company_id}")
    signals = conn.execute(
        "SELECT kind, note FROM signals WHERE company_id = ? "
        "ORDER BY id DESC LIMIT 3", (company_id,)).fetchall()
    return {"id": row["id"], "name": row["name"], "domain": row["domain"],
            "contact_email": row["contact_email"],
            "career_url": row["career_url"], "notes": row["notes"],
            "signals": [f"{s['kind']}: {(s['note'] or '')[:100]}"
                        for s in signals]}


def _resume_context() -> str:
    from jobscout.core.resume import load_master_resume

    r = load_master_resume()
    ident = r.identity
    exp = "; ".join(f"{e.title} @ {e.company}"
                    for e in (r.experience or [])[:3]) or "(none listed)"
    edu = "; ".join(f"{e.degree} {e.field or ''} @ {e.school}"
                    for e in (r.education or [])[:2]) or "(none listed)"
    skills = "; ".join(f"{a.area}: {', '.join((a.items or [])[:5])}"
                       for a in (r.skills.core or [])[:5])
    return (f"name: {ident.full_name}\n"
            f"current/latest roles: {exp}\n"
            f"education: {edu}\n"
            f"skills: {skills}")


def _profile_context() -> str:
    from jobscout.core.config import load_profile

    t = load_profile().target
    return (f"target roles: {', '.join(t.roles) or '(any)'}\n"
            f"levels: {', '.join(t.seniorities) or '(any)'}\n"
            f"locations: {', '.join(t.primary_locations or t.locations) or '(any)'}"
            + (f" (also: {', '.join(t.locations)})" if t.primary_locations else "")
            + f"\nstack: {', '.join(t.stack) or '(unspecified)'}")


# ── search (reuses the agent's engine chain) ────────────────────────────────


class _Ctx:
    """The minimal shape the search engines expect (settings/env/client)."""

    def __init__(self):
        import os

        from jobscout.core.config import load_env, load_settings
        from jobscout.sources.postings.base import make_client

        self.settings = load_settings()
        self.env = {**os.environ, **load_env()}
        self.client = make_client()


def _search(query: str, n: int, ctx: _Ctx) -> str | None:
    from jobscout.agent import tools

    chosen = (ctx.settings.search.provider
              if ctx.settings and ctx.settings.search else "auto")
    order = ["cse", "brave", "llm", "ddg"] if chosen == "auto" else [chosen]
    for name in order:
        fn = tools._engine(name)
        out = fn(query, n, ctx) if fn else None
        if out is not None:
            return out
    return None


def _parse_results(text: str) -> list[dict]:
    """Parse the engines' shared format: `N. title — url` + indented
    snippet lines."""
    out: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        m = re.match(r"^\d+\.\s+(.+?)\s+—\s+(\S+)$", line)
        if m:
            if cur:
                out.append(cur)
            cur = {"title": m.group(1), "url": m.group(2), "snippet": ""}
        elif cur is not None and line.startswith("   ") and line.strip():
            cur["snippet"] = (cur["snippet"] + " " + line.strip()).strip()
    if cur:
        out.append(cur)
    return out


def find_linkedin_contacts(company_name: str, ctx: _Ctx,
                            limit: int = 5) -> list[dict]:
    """Live web search for LinkedIn profiles at the company."""
    queries = [
        f'site:linkedin.com/in "{company_name}" '
        f'(recruiter OR "talent acquisition" OR hiring OR people)',
        f'"{company_name}" site:linkedin.com/in '
        f'(engineer OR developer OR manager)',
    ]
    seen: set[str] = set()
    contacts: list[dict] = []
    for q in queries:
        raw = _search(q, 8, ctx)
        for hit in _parse_results(raw or ""):
            url = hit["url"]
            if "linkedin.com/in/" not in url or url in seen:
                continue
            seen.add(url)
            name = hit["title"].split("—")[0].split("|")[0].strip()
            contacts.append({"name": name, "url": url,
                             "title": hit["title"][:120]})
            if len(contacts) >= limit:
                return contacts
    return contacts


# ── generation ──────────────────────────────────────────────────────────────


def _llm():
    from jobscout.clients.llm import LlmClient

    return LlmClient()


def _chat_json(system: str, user: str) -> tuple[dict, str, float]:
    resp = _llm().chat("quality", [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], json_mode=True)
    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text), resp.model, resp.cost_usd


def start_follow_up(packet_id: str) -> None:
    """Draft a follow-up email for a stale application (the nudge).
    Keyed by packet: pk-<id>:follow_up."""
    key = f"pk-{packet_id}:follow_up"
    if _state.get(key, {}).get("running"):
        return
    _state[key] = {"running": True, "error": ""}
    threading.Thread(target=_run_follow_up, args=(packet_id,),
                     daemon=True).start()


def _run_follow_up(packet_id: str) -> None:
    key = f"pk-{packet_id}:follow_up"
    conn = None
    try:
        conn = db.connect()
        _generate_follow_up(conn, packet_id)
    except Exception as e:                     # noqa: BLE001 — surfaced in UI
        _state[key] = {"running": False, "error": str(e)[:300]}
        if conn is not None:
            conn.close()
        return
    finally:
        st = _state.get(key, {})
        _state[key] = {"running": False, "error": st.get("error", "")}
    if conn is not None:
        conn.close()


def follow_up_running(packet_id: str) -> bool:
    return _state.get(f"pk-{packet_id}:follow_up", {}).get("running", False)


def _generate_follow_up(conn, packet_id: str) -> None:
    """A polite nudge: the application, its age, the timeline — drafted
    from the resume (honesty contract), appended to the account's DRAFTS
    when email tracking is linked (never sent)."""
    from datetime import UTC, datetime

    pk = conn.execute(
        "SELECT pk.id, pk.status, pk.applied_at, pk.updated_at, p.title,"
        " c.id AS company_id, c.name AS company_name, c.domain"
        " FROM packets pk JOIN postings p ON p.id = pk.posting_id"
        " JOIN companies c ON c.id = p.company_id"
        " WHERE pk.id = ?", (packet_id,)).fetchone()
    if pk is None:
        raise RuntimeError(f"unknown packet: {packet_id}")
    if not pk["applied_at"]:
        raise RuntimeError("no applied date — mark the application applied "
                           "first")
    events = conn.execute(
        "SELECT kind, event_date, title, notes FROM application_events"
        " WHERE packet_id = ? ORDER BY event_date DESC, id DESC LIMIT 5",
        (packet_id,)).fetchall()
    days = (datetime.now(UTC).date()
            - datetime.strptime(pk["applied_at"], "%Y-%m-%d").date()).days

    system = (
        "You draft short, polite follow-up emails for job applications. "
        "You may ONLY state facts from the provided master resume and "
        "timeline — never invent. Under 130 words, plain text, no "
        'placeholders. Output ONLY JSON: {"subject": "...", "body": "..."}.'
    )
    timeline = "\n".join(
        f"- {e['event_date']} {e['kind']}: {e['title'] or ''}"
        for e in events) or "(none recorded)"
    user = (
        f"APPLICATION: {pk['title']} at {pk['company_name']}"
        f" (applied {pk['applied_at']}, {days} days ago)"
        f"\nSTATE: {pk['status']}\nTIMELINE:\n{timeline}\n\n"
        f"CANDIDATE (master resume):\n{_resume_context()}\n\n"
        "Draft a follow-up: confirm continued interest, add ONE piece of "
        "true value (a relevant fact from the resume), and ask for a "
        "status update — gracious, not pushy."
    )
    draft, model, cost = _chat_json(system, user)

    # append to the mail Drafts when linked (draft-first: never sent)
    detail = {"days": days}
    try:
        from jobscout.webapp.runners import email_tracker

        user_email, pw, host = email_tracker._creds()
        if user_email and pw:
            from jobscout.clients.mail import MailClient

            MailClient(user_email, pw, host or None).append_draft(
                to_addr=_hr_address(conn, pk["company_id"]),
                subject=draft["subject"], body=draft["body"])
            detail["draft_saved"] = True
    except Exception as e:                     # noqa: BLE001 — best effort
        detail["draft_error"] = str(e)[:200]

    db.upsert_outreach(conn, company_id=pk["company_id"], kind="follow_up",
                       status="done",
                       content=json.dumps({**draft, "detail": detail}),
                       model=model, cost_usd=cost)


def _hr_address(conn, company_id: str) -> str:
    row = conn.execute(
        "SELECT contact_email, domain FROM companies WHERE id = ?",
        (company_id,)).fetchone()
    if row and row["contact_email"]:
        return row["contact_email"]
    if row and row["domain"]:
        return f"jobs@{row['domain']}"
    return "careers@example.com"                # user edits in their client


def _generate(conn, company_id: str, kind: str) -> None:
    company = _company_context(conn, company_id)
    resume_ctx = _resume_context()
    profile_ctx = _profile_context()

    if kind == "cold_email":
        system = (
            "You write concise, honest cold emails of interest for a job "
            "seeker. You may ONLY state facts that appear in the provided "
            "master resume or profile — never invent experience, numbers, "
            "or employers. Keep the body under 150 words, plain text, no "
            "placeholders like [Name]. Output ONLY JSON: "
            '{"subject": "...", "body": "..."}'
        )
        user = (
            f"COMPANY:\n{company['name']} ({company['domain'] or 'domain unknown'})\n"
            f"notes: {company['notes'] or '(none)'}\n"
            f"recent signals: {'; '.join(company['signals']) or '(none)'}\n\n"
            f"CANDIDATE (master resume):\n{resume_ctx}\n\n"
            f"HUNTING PROFILE:\n{profile_ctx}\n\n"
            "Draft a cold email of interest: who the candidate is, the "
            "strongest true fit signal, and a short ask (conversation or "
            "referral to the right team)."
        )
        draft, model, cost = _chat_json(system, user)
        db.upsert_outreach(conn, company_id=company_id, kind=kind,
                           status="done",
                           content=json.dumps(draft), model=model,
                           cost_usd=cost)

    elif kind == "linkedin":
        ctx = _Ctx()
        contacts = find_linkedin_contacts(company["name"], ctx)
        if not contacts:
            db.upsert_outreach(
                conn, company_id=company_id, kind=kind, status="failed",
                error="no LinkedIn profiles found via search — try again "
                      "later or search manually on linkedin.com")
            return
        system = (
            "You draft LinkedIn outreach for a job seeker. You may ONLY "
            "state facts from the provided master resume or profile — "
            "never invent. connection_note must be <= 300 characters. "
            "Output ONLY JSON: "
            '{"contacts": [{"name": "...", "url": "...", '
            '"connection_note": "...", "message": "..."}]} — one entry '
            "per provided contact, keeping the given name/url."
        )
        user = (
            f"COMPANY:\n{company['name']} ({company['domain'] or 'unknown'})\n"
            f"CANDIDATE (master resume):\n{resume_ctx}\n\n"
            f"HUNTING PROFILE:\n{profile_ctx}\n\n"
            f"LINKEDIN CONTACTS FOUND (use exactly these):\n"
            + "\n".join(f"- {c['name']} — {c['url']} ({c['title']})"
                        for c in contacts)
            + "\n\nDraft a connection_note and a follow-up message for "
              "each contact: who the candidate is, one true fit signal, "
              "a specific, short ask."
        )
        draft, model, cost = _chat_json(system, user)
        db.upsert_outreach(conn, company_id=company_id, kind=kind,
                           status="done",
                           content=json.dumps(draft), model=model,
                           cost_usd=cost)
