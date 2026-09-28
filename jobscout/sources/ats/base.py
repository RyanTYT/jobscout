"""Shared plumbing for ATS board connectors (PLAN §6.1)."""

from __future__ import annotations

import re
import time

import httpx
from pydantic import BaseModel


class SourceError(Exception):
    """A source fetch failed (network/parse)."""


class SourceNotFound(SourceError):
    """The board/token does not exist (404) — not an outage."""


class RawPosting(BaseModel):
    source: str  # e.g. "ats:greenhouse:drw"
    company: str
    company_slug: str
    title: str
    url: str
    location: str | None = None
    remote: bool | None = None
    description: str | None = None
    posted_at: str | None = None
    job_type: str | None = None
    department: str | None = None
    seniority: str | None = None


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = html
    for tag in ("<br", "</p", "</li", "</h1", "</h2", "</h3", "</h4", "</div", "</tr"):
        text = text.replace(tag, "\n" + tag)
    text = _TAG_RE.sub(" ", text)
    for ent, ch in (
        ("&amp;", "&"), ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&#x27;", "'"), ("&rsquo;", "'"),
        ("&lsquo;", "'"), ("&ndash;", "-"), ("&mdash;", "-"),
    ):
        text = text.replace(ent, ch)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={
            "User-Agent": "jobscout/0.1 (personal job monitoring)",
            "Accept": "application/json",
        },
        follow_redirects=True,
    )


def soft_get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response | None:
    """GET with soft failure: None on network error / non-200 / empty body."""
    try:
        r = client.get(url, params=params)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not r.text:
        return None
    return r


def polite(min_seconds: float = 0.25) -> None:
    time.sleep(min_seconds)
