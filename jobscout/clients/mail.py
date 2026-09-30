"""clients/mail.py — the IMAP client (email tracking + drafts).

Gmail and Outlook personal accounts both speak IMAP with app-specific
passwords (Gmail: 2FA + app password; Outlook: account password or app
password depending on account age). OAuth is deliberately NOT supported
— it would need a Google Cloud project / Azure app registration for a
personal local app, which is the wrong trade.

SAFETY PROPERTY: this client can FETCH and APPEND DRAFTS only — IMAP
has no send. Every reply the app prepares lands in the account's
Drafts mailbox for the human to review and send from their own mail
client. Nothing here can ever send mail.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import time
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

# domains whose notifications carry hiring email (ATS senders)
ATS_SENDER_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "workday.com", "myworkdayjobs.com", "icims.com", "successfactors.com",
    "taleo.net", "eightfold.ai", "rippling.com", "greenhousemail.com",
)

_HOST_BY_DOMAIN = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
}


class MailError(Exception):
    pass


def detect_host(user: str) -> str | None:
    """imap host from the account's domain (None when unknown — the
    JOBSCOUT_EMAIL_HOST override or an explicit host is then required)."""
    domain = (user or "").rpartition("@")[2].lower()
    return _HOST_BY_DOMAIN.get(domain)


class MailClient:
    """One IMAP connection per operation (thread-simple, stateless)."""

    def __init__(self, user: str, password: str, host: str | None = None):
        self.user = user
        self.host = host or detect_host(user)
        if not self.user or not password:
            raise MailError("email account not configured "
                            "(JOBSCOUT_EMAIL_USER / JOBSCOUT_EMAIL_PASS)")
        if not self.host:
            raise MailError(
                f"no IMAP host known for {user} — set JOBSCOUT_EMAIL_HOST "
                "in .env (e.g. imap.gmail.com)")
        self.password = password

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            m = imaplib.IMAP4_SSL(self.host, 993)
            m.login(self.user, self.password)
            return m
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"IMAP login failed: {e}") from e

    def test(self) -> list[str]:
        """Login + list mailboxes (connection check)."""
        m = self._connect()
        try:
            return self._mailbox_names(m)
        finally:
            m.logout()

    # ── fetch ───────────────────────────────────────────────────────────

    def fetch_new(self, last_uid: int, limit: int = 200) -> list[dict]:
        """Messages with UID > last_uid (oldest first), capped at limit."""
        m = self._connect()
        try:
            m.select("INBOX", readonly=True)
            typ, data = m.uid("search", None, "ALL")
            if typ != "OK":
                raise MailError("mailbox search failed")
            uids = [int(u) for u in (data[0] or b"").split() if u]
            new = [u for u in uids if u > (last_uid or 0)][-limit:]
            out: list[dict] = []
            for uid in new:
                typ, d = m.uid("fetch", str(uid).encode(), "(RFC822)")
                if typ != "OK" or not d or d[0] is None:
                    continue
                raw = d[0][1] if isinstance(d[0], tuple) else None
                if raw is None:
                    continue
                out.append(self._parse(uid, raw))
            return out
        finally:
            m.logout()

    def _parse(self, uid: int, raw: bytes) -> dict:
        msg = email_lib.message_from_bytes(raw)
        from_addr = parseaddr(msg.get("From", ""))[1]
        subject = self._decode(msg.get("Subject", ""))
        date_hdr = msg.get("Date", "")
        sent_at = None
        if date_hdr:
            try:
                sent_at = parsedate_to_datetime(date_hdr).isoformat()
            except (TypeError, ValueError):
                pass
        body = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except (AttributeError, LookupError):
                    pass
                break
        return {"uid": uid, "from_addr": from_addr,
                "from_domain": from_addr.rpartition("@")[2].lower(),
                "subject": subject, "sent_at": sent_at,
                "message_id": (msg.get("Message-ID") or "").strip(),
                "body": (body or "")[:4000]}

    def _decode(self, value: str) -> str:
        from email.header import decode_header

        parts = []
        for text, enc in decode_header(value or ""):
            if isinstance(text, bytes):
                parts.append(text.decode(enc or "utf-8", "replace"))
            else:
                parts.append(text)
        return "".join(parts)

    # ── drafts (APPEND only — IMAP cannot send) ─────────────────────────

    def _mailbox_names(self, m) -> list[str]:
        names = []
        for line in (m.list() or (None, []))[1] or []:
            if isinstance(line, bytes):
                try:
                    names.append(line.decode("utf-8", "replace"))
                except Exception:                     # noqa: BLE001
                    continue
        return names

    def drafts_mailbox(self, m) -> str:
        """The account's Drafts mailbox ('[Gmail]/Drafts', 'Drafts', ...)."""
        names = self._mailbox_names(m)
        import re

        quoted = []
        for n in names:
            mm = re.findall(r'"((?:[^"\\]|\\.)*)"', n)
            if mm:
                quoted.append(mm[-1])
        candidates = [q for q in quoted if "draft" in q.lower()]
        if not candidates:
            # UTF-7 style [Gmail]/Drafts sometimes appears unquoted
            candidates = [n for n in names if "draft" in n.lower()]
        if not candidates:
            raise MailError("no Drafts mailbox found on the account")
        for c in candidates:
            if c.lower() == "drafts":
                return c
        return sorted(candidates)[0]

    def append_draft(self, to_addr: str, subject: str, body: str, *,
                     in_reply_to: str | None = None,
                     from_name: str = "") -> str:
        """Create a DRAFT in the account's Drafts mailbox (never sent —
        the human reviews and sends from their own mail client)."""
        msg = EmailMessage()
        name = (from_name or "").strip() or self.user
        msg["From"] = f"{name} <{self.user}>"
        msg["To"] = to_addr
        msg["Subject"] = subject or "(draft)"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg["X-Jobscout-Draft"] = "true"
        msg.set_content(body)
        raw = msg.as_bytes()
        m = self._connect()
        try:
            mailbox = self.drafts_mailbox(m)
            m.append(mailbox, "\\Draft",
                     imaplib.Time2Internaldate(time.time()), raw)
            return mailbox
        except imaplib.IMAP4.error as e:
            raise MailError(f"draft append failed: {e}") from e
        finally:
            m.logout()
