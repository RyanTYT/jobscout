"""Greenhouse boards API — free, no auth (PLAN §6.1).

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Bad tokens return 404, which makes probing reliable.
"""

from __future__ import annotations

import httpx

from jobscout.sources.postings.base import RawPosting, SourceError, SourceNotFound, strip_html

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


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
        r = client.get(url, params={"content": "true" if content else "false"})
    except httpx.HTTPError as e:
        raise SourceError(f"greenhouse {token}: {e}") from e
    if r.status_code in (404, 400):
        raise SourceNotFound(f"greenhouse board not found: {token}")
    if r.status_code != 200:
        raise SourceError(f"greenhouse {token}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise SourceError(f"greenhouse {token}: invalid JSON") from e

    out: list[RawPosting] = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        link = (j.get("absolute_url") or "").strip()
        if not title or not link:
            continue
        location = ((j.get("location") or {}).get("name")) or None
        offices = ", ".join(
            (o.get("name") or "") for o in (j.get("offices") or []) if o.get("name")
        )
        departments = ", ".join(
            (d.get("name") or "") for d in (j.get("departments") or []) if d.get("name")
        )
        out.append(
            RawPosting(
                source=f"ats:greenhouse:{token}",
                company=company,
                company_slug=company_slug,
                title=title,
                url=link,
                location=location or (offices or None),
                remote=any(
                    "remote" in ((o.get("name") or "").lower()) for o in (j.get("offices") or [])
                )
                or None,
                description=(strip_html(j.get("content"))[:4000] or None) if content else None,
                posted_at=(j.get("first_published") or j.get("updated_at") or None),
                department=departments or None,
            )
        )
    return out
