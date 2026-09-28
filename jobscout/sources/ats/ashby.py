"""Ashby posting API — free, no auth (PLAN §6.1).

GET https://api.ashbyhq.com/posting-api/job-board/{org}
Bad orgs return 404.
"""

from __future__ import annotations

import httpx

from jobscout.sources.ats.base import RawPosting, SourceError, SourceNotFound, strip_html

BASE = "https://api.ashbyhq.com/posting-api/job-board/{org}"


def _location_str(loc) -> str | None:
    if loc is None:
        return None
    if isinstance(loc, str):
        return loc.strip() or None
    if isinstance(loc, dict):
        parts = [loc.get("city"), loc.get("region") or loc.get("state"), loc.get("country")]
        s = ", ".join(p for p in parts if p)
        return s or None
    return None


def _departments_str(deps) -> str | None:
    if not deps:
        return None
    names = []
    for d in deps:
        if isinstance(d, str):
            names.append(d)
        elif isinstance(d, dict):
            names.append(d.get("name") or "")
    s = ", ".join(n for n in names if n)
    return s or None


def fetch(
    org: str,
    client: httpx.Client,
    *,
    company: str,
    company_slug: str,
    content: bool = True,
) -> list[RawPosting]:
    url = BASE.format(org=org)
    try:
        r = client.get(url, params={"includeCompensation": "false"})
    except httpx.HTTPError as e:
        raise SourceError(f"ashby {org}: {e}") from e
    if r.status_code in (404, 400):
        raise SourceNotFound(f"ashby org not found: {org}")
    if r.status_code != 200:
        raise SourceError(f"ashby {org}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise SourceError(f"ashby {org}: invalid JSON") from e

    out: list[RawPosting] = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        link = (j.get("applyUrl") or j.get("jobUrl") or "").strip()
        if not title or not link:
            continue
        is_remote = j.get("isRemote")
        out.append(
            RawPosting(
                source=f"ats:ashby:{org}",
                company=company,
                company_slug=company_slug,
                title=title,
                url=link,
                location=_location_str(j.get("location")),
                remote=bool(is_remote) if is_remote is not None else None,
                description=(strip_html(j.get("description"))[:4000] or None) if content else None,
                posted_at=(
                    j.get("publishedAt") or j.get("publishedDate") or j.get("updatedAt") or None
                ),
                job_type=j.get("employmentType") or j.get("type") or None,
                department=_departments_str(j.get("departments") or j.get("teams")),
            )
        )
    return out
