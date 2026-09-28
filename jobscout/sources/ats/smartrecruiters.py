"""SmartRecruiters public API — free, no auth (PLAN §6.1).

GET https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100&offset=N
Company ids are display-cased (e.g. "Citadel"). Bad ids 404; some wrong ids
can 200 with an empty list, so probing requires jobs > 0.
Postings list has no description — detail fetch is deferred to P2.
"""

from __future__ import annotations

import httpx

from jobscout.sources.ats.base import RawPosting, SourceError, SourceNotFound, polite

BASE = "https://api.smartrecruiters.com/v1/companies/{cid}/postings"

MAX_PAGES = 3  # 300 postings per company — enough for P1


def fetch(
    cid: str,
    client: httpx.Client,
    *,
    company: str,
    company_slug: str,
    content: bool = True,
) -> list[RawPosting]:
    out: list[RawPosting] = []
    for page in range(MAX_PAGES):
        url = BASE.format(cid=cid)
        try:
            r = client.get(url, params={"limit": 100, "offset": page * 100})
        except httpx.HTTPError as e:
            raise SourceError(f"smartrecruiters {cid}: {e}") from e
        if r.status_code in (404, 400):
            raise SourceNotFound(f"smartrecruiters company not found: {cid}")
        if r.status_code != 200:
            raise SourceError(f"smartrecruiters {cid}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise SourceError(f"smartrecruiters {cid}: invalid JSON") from e
        batch = data.get("content") or []
        for j in batch:
            title = (j.get("name") or "").strip()
            pid = (j.get("id") or "").strip()
            if not title or not pid:
                continue
            loc = j.get("location") or {}
            location = ", ".join(
                p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
            )
            out.append(
                RawPosting(
                    source=f"ats:smartrecruiters:{cid}",
                    company=company,
                    company_slug=company_slug,
                    title=title,
                    url=f"https://jobs.smartrecruiters.com/{cid}/{pid}",
                    location=location or None,
                    remote=True if "remote" in location.lower() else None,
                    description=None,  # detail endpoint deferred to P2
                    posted_at=j.get("releasedDate") or None,
                    job_type=(j.get("typeOfEmployment") or {}).get("label") or None,
                    department=(j.get("department") or {}).get("label") or None,
                )
            )
        if len(batch) < 100:
            break
        polite(0.25)
    return out
