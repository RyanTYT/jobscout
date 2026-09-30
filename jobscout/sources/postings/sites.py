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


# ── the sidecar seam: bot-walled boards via the JobPilot Playwright ──────
# LinkedIn/Wellfound/YC 403 plain HTTP (bot walls) — they need the real
# browser. These are scraped as ONE batch (the orchestrator runs the
# scrapers in parallel, one browser pool) per role query.
# SITES (above) is the seam for plain-API sites; this is the seam for
# Playwright sites. Both feed the same sweep.

SIDECAR_SCRAPER_IDS = ("linkedin", "wellfound", "ycombinator")

# sidecar SeniorityLevel → jobscout rule-gate tokens. "mid" maps to None
# (treated as unlabeled — it passes the gate to LLM scoring; the user's
# junior/mid profile should see mid postings); unknown levels also None.
_SENIORITY_MAP = {
    "intern": "intern", "junior": "junior", "senior": "senior",
    "staff": "staff", "principal": "principal", "lead": "lead",
    "manager": "manager",
}

_sidecar_client = None


def _get_sidecar():
    """The shared sidecar for site scraping (lazy, one per sweep run)."""
    global _sidecar_client
    if _sidecar_client is None:
        from jobscout.clients.sidecar import SidecarClient

        _sidecar_client = SidecarClient(headless=True)
        _sidecar_client.start()
    return _sidecar_client


def _stop_sidecar() -> None:
    """Shut the shared sidecar down (the sweep's finally path)."""
    global _sidecar_client
    if _sidecar_client is not None:
        try:
            _sidecar_client.stop()
        except Exception:                   # noqa: BLE001 — best effort
            pass
        _sidecar_client = None


def sidecar_scrape(query: str, location: str, scraper_ids: tuple[str, ...],
                   top_n: int = 50) -> list[RawPosting]:
    """One batched sidecar scrape → RawPostings. Degrades to [] when the
    sidecar isn't built (jobscout keeps working, HTTP sites only)."""
    from jobscout.clients.sidecar import SidecarError

    try:
        client = _get_sidecar()
        jobs = client.scrape_sites(list(scraper_ids), keywords=query,
                                   location=location, top_n=top_n)
    except (SidecarError, OSError):
        return []
    out: list[RawPosting] = []
    for j in jobs or []:
        title = (j.get("title") or "").strip()
        url = (j.get("applyUrl") or "").strip()
        company = (j.get("company") or "unknown").strip()
        if not title or not url:
            continue
        host = (j.get("applyHostname") or "").strip() or "sidecar"
        seniority = _SENIORITY_MAP.get(
            (j.get("seniority") or "").strip().lower())
        out.append(RawPosting(
            source=f"site:{host}", title=title, url=url,
            company=company, company_slug=_slug(company),
            location=(j.get("location") or location or None),
            remote=bool(j.get("remote")) or None,
            description=(j.get("description") or "")[:2000] or None,
            seniority=seniority,
            posted_at=str(j["postedAt"])[:10] if j.get("postedAt") else None,
        ))
    return out


# the registry: name -> fn(query, location, client) -> [RawPosting]
# (plain-API sites; the sidecar sites live in SIDECAR_SCRAPER_IDS above)
SITES: dict[str, object] = {
    "mycareersfuture": mcf_search,
}


def sweep_sites(client: httpx.Client, profile) -> list[RawPosting]:
    """Search every registered site with the profile's roles × locations.

    Two seams: plain-API sites (SITES) per role×location, and ONE
    batched sidecar scrape per role (all Playwright boards in parallel —
    the browser pool is shared, so batching is far cheaper than per-site
    spawns). Capped so one sweep stays polite.
    """
    out: list[RawPosting] = []
    seen_urls: set[str] = set()
    roles = (profile.target.roles or ["software engineer"])[:3]
    locations = (profile.target.primary_locations
                 or profile.target.locations or ["Singapore"])[:2]

    def _add(p: RawPosting) -> None:
        if p.url not in seen_urls:
            seen_urls.add(p.url)
            out.append(p)

    for site_fn in SITES.values():
        for role in roles:
            for location in locations:
                try:
                    for p in site_fn(role, location, client):
                        _add(p)
                except Exception:               # noqa: BLE001 — degrade
                    continue

    # the sidecar batch (LinkedIn + Wellfound + YC) — only when built
    from jobscout.clients.sidecar import SidecarClient

    if SIDECAR_SCRAPER_IDS and SidecarClient.available():
        primary = locations[0] if locations else ""
        try:
            for role in roles[:2]:            # 2 browser batches per sweep
                for p in sidecar_scrape(role, primary, SIDECAR_SCRAPER_IDS,
                                        top_n=50):
                    _add(p)
        finally:
            _stop_sidecar()                    # never leave a browser running
    return out
