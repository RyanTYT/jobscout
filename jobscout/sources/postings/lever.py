"""Lever postings API — free, no auth (PLAN §6.1).

GET https://api.lever.co/v0/postings/{company}?mode=json
Bad company ids return 404.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from jobscout.sources.postings.base import RawPosting, SourceError, SourceNotFound, strip_html

BASE = "https://api.lever.co/v0/postings/{token}"


def _iso_from_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def fetch(
    token: str,
    client: httpx.Client,
    *,
    company: str,
    company_slug: str,
    content: bool = True,
) -> list[RawPosting]:
    url = BASE.format(token=token)
    try:
        r = client.get(url, params={"mode": "json"})
    except httpx.HTTPError as e:
        raise SourceError(f"lever {token}: {e}") from e
    if r.status_code == 404:
        raise SourceNotFound(f"lever company not found: {token}")
    if r.status_code != 200:
        raise SourceError(f"lever {token}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise SourceError(f"lever {token}: invalid JSON") from e
    if not isinstance(data, list):
        raise SourceError(f"lever {token}: unexpected payload shape")

    out: list[RawPosting] = []
    for p in data:
        title = (p.get("text") or "").strip()
        link = (p.get("hostedUrl") or "").strip()
        if not title or not link:
            continue
        cats = p.get("categories") or {}
        desc = strip_html(p.get("description") or p.get("descriptionPlain"))
        out.append(
            RawPosting(
                source=f"ats:lever:{token}",
                company=company,
                company_slug=company_slug,
                title=title,
                url=link,
                location=cats.get("location") or None,
                remote=None,
                description=(desc[:4000] or None) if content else None,
                posted_at=_iso_from_ms(p.get("createdAt")),
                job_type=cats.get("commitment") or None,
                department=cats.get("team") or cats.get("department") or None,
            )
        )
    return out
