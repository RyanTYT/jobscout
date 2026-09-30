"""sources/postings/sites.py — keyword-search job sites (public APIs).

Unlike the board fetchers (per-company tokens), these search by keyword
against the hunting profile — roles × locations — so they feed the
inbox with market-wide matches, not just watchlist companies.

MyCareersFuture (Singapore): the official government board with a
DOCUMENTED public JSON API — the strongest source and the perfect fit
for the Singapore-first hunting profile.

The others the user asked about (LinkedIn, Indeed, Glints) are
bot-walled or undocumented: plain HTTP gets 403s (LinkedIn/Indeed run
aggressive bot detection) — they need the JobPilot Playwright sidecar
(next build). SITES is the registry: add a fetcher here and the daily
sweep picks it up.
"""

from __future__ import annotations

import re

import httpx

from jobscout.sources.postings.base import RawPosting, soft_get

MCF_API = "https://api.mycareersfuture.gov.sg/v2/jobs"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def mcf_search(query: str, location: str, client: httpx.Client,
               max_pages: int = 2) -> list[RawPosting]:
    """MyCareersFuture v2 — the official Singapore government
    board's public JSON API (free, no auth; verified live 2026-09-29)."""
    postings: list[RawPosting] = []
    for page in range(0, max_pages):
        r = soft_get(client, MCF_API,
                     params={"search": query, "location": location,
                             "page": page, "limit": 100})
        if r is None:
            break
        try:
            data = r.json()
        except ValueError:
            break
        results = data.get("results") or []
        if not results:
            break
        for j in results:
            title = _clean(j.get("title"))
            if not title:
                continue
            company = ((j.get("postedCompany") or {}).get("name")
                       or "unknown")
            meta = j.get("metadata") or {}
            job_id = meta.get("jobPostId") or j.get("uuid")
            if not job_id:
                continue
            url = f"https://www.mycareersfuture.gov.sg/sg/job/{job_id}"
            desc = _clean(j.get("description") or "")[:2000]
            postings.append(RawPosting(
                source="site:mycareersfuture",
                title=title, url=url,
                company=company, company_slug=_slug(company),
                location=location, description=desc))
        if len(results) < 100:
            break
    return postings


# the registry: name -> fn(query, location, client) -> [RawPosting]
SITES: dict[str, object] = {
    "mycareersfuture": mcf_search,
}


def sweep_sites(client: httpx.Client, profile) -> list[RawPosting]:
    """Search every registered site with the profile's roles × locations.
    Capped so one sweep stays polite (2 pages per role×location)."""
    out: list[RawPosting] = []
    seen_urls: set[str] = set()
    roles = (profile.target.roles or ["software engineer"])[:3]
    locations = (profile.target.primary_locations
                 or profile.target.locations or ["Singapore"])[:2]
    for site_fn in SITES.values():
        for role in roles:
            for location in locations:
                try:
                    for p in site_fn(role, location, client):
                        if p.applyUrl not in seen_urls:
                            seen_urls.add(p.applyUrl)
                            out.append(p)
                except Exception:               # noqa: BLE001 — degrade
                    continue
    return out
