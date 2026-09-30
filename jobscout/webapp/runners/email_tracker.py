"""webapp/runners/email_tracker.py — the follow-through watcher.

Polls the linked IMAP account (every N minutes while the app runs; a
manual "check now" button too), filters new email to senders matching
the companies you have LIVE applications with, classifies each with the
cheap cached LLM tier, and moves packet states forward (applied →
interviewing → offer; rejections land too, never from an offer).

DRAFT-FIRST, ALWAYS: when an email needs a reply (interview invite,
request for more info), the reply is DRAFTED by the quality tier and
APPENDED to the account's Drafts mailbox via IMAP — the app cannot
send mail at all; you review the draft in your own mail client and
press send there. Nothing outbound happens without that human click.
"""

from __future__ import annotations

import json
import threading
import time

from jobscout.clients.mail import ATS_SENDER_DOMAINS, MailError
from jobscout.core import db

_state: dict = {"running": False, "last_check": "", "last_error": "",
                "checked": 0, "matched": 0}

# monotonic funnel ranks — auto-updates only move forward
_RANK = {"packet:drafting": 0, "packet:needs_input": 1, "packet:ready": 2,
         "filled": 3, "applied": 4, "interviewing": 5, "offer": 6,
         "rejected": 7, "withdrawn": 8}
_CLASS_TO_STATUS = {"interview_invite": "interviewing", "offer": "offer",
                    "rejection": "rejected"}
# emails worth drafting a reply for
_ACTIONABLE = ("interview_invite", "request_more_info")


# ── lifecycle ───────────────────────────────────────────────────────────────


def state() -> dict:
    return dict(_state)


def enabled() -> bool:
    from jobscout.core.config import load_settings

    try:
        return load_settings().email.enabled
    except Exception:                            # noqa: BLE001
        return False


def maybe_start() -> bool:
    """Start the poll loop when email tracking is on (idempotent)."""
    if not enabled() or _state["running"]:
        return False
    _state["running"] = True
    threading.Thread(target=_loop, daemon=True).start()
    return True


def check_now() -> None:
    """One immediate check (the manual button)."""
    threading.Thread(target=_safe_check, daemon=True).start()


def _loop() -> None:
    while True:
        from jobscout.core.config import load_settings

        try:
            s = load_settings()
        except Exception:                        # noqa: BLE001
            s = None
        if s is None or not s.email.enabled:
            _state["running"] = False
            return
        _safe_check()
        time.sleep(max(1, s.email.poll_minutes) * 60)


def _safe_check() -> None:
    try:
        summary = check_once()
        _state["last_error"] = ""
        _state["last_check"] = time.strftime("%Y-%m-%d %H:%M")
        _state["checked"] = summary["checked"]
        _state["matched"] = summary["matched"]
    except MailError as e:
        _state["last_error"] = str(e)[:300]
    except Exception as e:                       # noqa: BLE001 — surfaced in UI
        _state["last_error"] = f"check failed: {e}"[:300]


# ── credentials (env, like every other secret) ──────────────────────────────


def _creds() -> tuple[str, str, str]:
    import os

    from jobscout.core.config import load_env

    env = {**os.environ, **load_env()}
    return (env.get("JOBSCOUT_EMAIL_USER") or "").strip(), \
        (env.get("JOBSCOUT_EMAIL_PASS") or "").strip(), \
        (env.get("JOBSCOUT_EMAIL_HOST") or "").strip()


# ── one check ───────────────────────────────────────────────────────────────


def check_once() -> dict:
    """Fetch new mail, match, classify, update states, draft replies."""
    from jobscout.clients.mail import MailClient

    user, password, host = _creds()
    client = MailClient(user, password, host or None)
    conn = db.connect()
    try:
        last_uid = int(db.get_state(conn, "email_last_uid") or 0)
        mails = client.fetch_new(last_uid)
        if not mails:
            return {"checked": 0, "matched": 0}
        companies = _active_companies(conn)
        matched = [(m, c) for m in mails for c in (_match(m, companies),) if c]
        classified = []
        for m, company in matched:
            cls, confidence = _classify(m)
            if cls is None:
                continue
            _apply(conn, m, company, cls, confidence, user, client)
            classified.append(m["uid"])
        db.set_state(conn, "email_last_uid", str(max(m["uid"] for m in mails)))
        return {"checked": len(mails), "matched": len(matched)}
    finally:
        conn.close()


def _active_companies(conn) -> list[dict]:
    """Companies with a live application (filled/applied/interviewing)."""
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.domain, p.id AS packet_id, p.status
        FROM companies c
        JOIN postings po ON po.company_id = c.id
        JOIN packets p ON p.posting_id = po.id
        WHERE p.status IN ('filled', 'applied', 'interviewing')
        ORDER BY p.updated_at DESC
        """
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["id"], {"id": r["id"], "name": r["name"],
                                 "domain": (r["domain"] or "").lower(),
                                 "packet_ids": [], "status": r["status"]})
        out[r["id"]]["packet_ids"].append((r["packet_id"], r["status"]))
    return list(out.values())


def _match(mail: dict, companies: list[dict]) -> dict | None:
    """The company an email belongs to: same domain (or subdomain), or an
    ATS sender whose subject/body names the company."""
    d = mail["from_domain"]
    text = f"{mail['subject']} {mail['body']}".lower()
    for c in companies:
        dom = c["domain"]
        if dom and (d == dom or d.endswith("." + dom)):
            return c
        if (any(d == dom or d.endswith("." + dom)
                for dom in ATS_SENDER_DOMAINS)
                and c["name"].lower() in text):
            return c
    return None


def _classify(mail: dict) -> tuple[str | None, str]:
    """Cheap cached classification of one email (bulk tier)."""
    from jobscout.clients.llm import LlmClient, LlmError

    content = (f"from: {mail['from_addr']}\nsubject: {mail['subject']}\n\n"
               f"{mail['body'][:800]}")
    system = (
        "You classify job-application emails. Output ONLY JSON: "
        '{"class": "...", "confidence": "high|low"}. Classes: '
        "interview_invite (interview scheduled/requested, assessment, "
        "next round), offer (job offer), rejection (rejection/decline/"
        "moving forward with other candidates), request_more_info (asks "
        "the candidate for something: availability, documents, questions), "
        "auto_reply (acknowledgement/confirmation/status page link), "
        "other (anything else, including newsletters and non-job mail)."
    )
    try:
        resp = LlmClient().chat("bulk", [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ], json_mode=True, cache_key=mail["message_id"] or None)
        text = (resp.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        data = json.loads(text)
        return data.get("class"), str(data.get("confidence", "low"))
    except (LlmError, ValueError):
        return None, ""


def _apply(conn, mail: dict, company: dict, cls: str, confidence: str,
           user: str, client) -> None:
    """Record the event; move the state forward; draft the reply when
    the email is actionable (draft lands in the account's Drafts)."""
    packet_id, new_status = _advance(conn, company, cls)
    detail: dict = {"confidence": confidence}
    action = "no change"

    if new_status:
        action = f"state: {new_status}"
        detail["new_status"] = new_status

    if cls in _ACTIONABLE and packet_id:
        draft = _draft_reply(conn, mail, company, user)
        if draft:
            try:
                mailbox = client.append_draft(
                    to_addr=mail["from_addr"], subject=draft["subject"],
                    body=draft["body"], in_reply_to=mail["message_id"] or None,
                    from_name=_candidate_name(conn))
                action = (f"{action}; draft saved" if new_status
                          else "draft saved")
                detail["draft_subject"] = draft["subject"]
                detail["draft_body"] = draft["body"][:2000]
                detail["draft_mailbox"] = mailbox
            except MailError as e:
                detail["draft_error"] = str(e)[:200]

    db.record_email_event(
        conn, company_id=company["id"], packet_id=packet_id,
        from_addr=mail["from_addr"], subject=mail["subject"],
        sent_at=mail["sent_at"], message_id=mail["message_id"],
        classification=cls, action=action,
        detail=json.dumps(detail))


def _advance(conn, company: dict, cls: str) -> tuple[str | None, str | None]:
    """Monotonic state move for the company's most-recent live packet.
    Returns (packet_id, new_status) — (None, None) when nothing moves."""
    target = _CLASS_TO_STATUS.get(cls)
    if not target or not company["packet_ids"]:
        return company["packet_ids"][0][0] if company["packet_ids"] else None, \
            None
    # the most recent live packet is the active application
    packet_id, current = company["packet_ids"][0]
    cur_rank = _RANK.get(current, -1)
    tgt_rank = _RANK[target]
    if cls == "rejection":
        # rejections land from any live pre-offer state; never auto-demote
        # an offer (a human should read that one)
        if cur_rank >= _RANK["offer"] or current == "withdrawn":
            return packet_id, None
        if current == "rejected":
            return packet_id, None
        db.set_packet_status(conn, packet_id, target)
        return packet_id, target
    if tgt_rank > cur_rank and current != "withdrawn":
        db.set_packet_status(conn, packet_id, target)
        return packet_id, target
    return packet_id, None


def _candidate_name(conn) -> str:
    try:
        from jobscout.core.resume import load_master_resume

        return load_master_resume().identity.full_name or ""
    except Exception:                            # noqa: BLE001
        return ""


def _draft_reply(conn, mail: dict, company: dict, user: str) -> dict | None:
    """Quality-tier reply draft — grounded in the resume, honest, short."""
    from jobscout.clients.llm import LlmClient, LlmError

    try:
        resume = _resume_line(conn)
        system = (
            "You draft short, honest replies to job-application emails. "
            "You may ONLY state facts from the provided master resume — "
            "never invent. Under 120 words, plain text, no placeholders. "
            'Output ONLY JSON: {"subject": "...", "body": "..."}. Subject '
            "keeps the original thread: 'Re: <original subject>'."
        )
        user_msg = (
            f"COMPANY: {company['name']}\n"
            f"ORIGINAL EMAIL:\nfrom: {mail['from_addr']}\n"
            f"subject: {mail['subject']}\n"
            f"{mail['body'][:900]}\n\n"
            f"CANDIDATE (master resume):\n{resume}\n\n"
            "Draft the reply: confirm enthusiasm, answer or acknowledge "
            "what they asked, state availability honestly if the resume "
            "says it, and nothing else."
        )
        resp = LlmClient().chat("quality", [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], json_mode=True)
        text = (resp.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        data = json.loads(text)
        subject = data.get("subject") or f"Re: {mail['subject']}"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {mail['subject']}"
        return {"subject": subject, "body": data.get("body", "")}
    except (LlmError, ValueError):
        return None


def _resume_line(conn) -> str:
    try:
        from jobscout.core.resume import load_master_resume

        r = load_master_resume()
        exp = "; ".join(f"{e.title} @ {e.company}"
                        for e in (r.experience or [])[:2])
        return f"{r.identity.full_name} — {exp}"
    except Exception:                            # noqa: BLE001
        return "(resume unavailable)"
