"""Careers-page crawler — the dark-pool detector's eyes (PLAN §6.1, §10 P3).

Static-first ladder, stdlib only (no new dependencies):

  1. careers-URL discovery: careers./jobs. subdomains → common paths →
     homepage anchor scan
  2. board-link scan: ATS tokens found in page HTML are VERIFIED live —
     a discovered board upgrades the company out of the dark pool and
     self-heals the watchlist (§5.9)
  3. JSON-LD `JobPosting` extraction from the careers page
  4. WordPress Job Manager feeds (/jobs/feed/, /feed/?post_type=job_listing)
  5. bounded sitemap crawl of job-detail URLs → JSON-LD per page

JS-rendered SPA careers pages yield nothing here — recorded honestly as
"requires sidecar render (P7)" and skipped. The ladder is ordered by
cheapness; the first rung that produces postings wins.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx

from jobscout.core.db import slugify
from jobscout.sources.ats import FETCHERS
from jobscout.sources.ats.base import (
    RawPosting,
    SourceError,
    SourceNotFound,
    strip_html,
)
from jobscout.sources.ats.base import (
    soft_get as _get,
)

_JSONLD_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_CAREERS_ANCHOR_RE = re.compile(r"career|jobs?\b|join|vacanc|hiring|opportunit", re.I)

BOARD_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I
    ),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I),  # ashby orgs may contain dots
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I),
}

_CAREERS_PATHS = (
    "/careers", "/jobs", "/careers/jobs", "/join-us", "/join", "/work-with-us",
    "/about/careers", "/company/careers", "/open-roles", "/openings",
    "/careers.html", "/jobs.html",
)
_WP_FEED_PATHS = ("/jobs/feed/", "/feed/?post_type=job_listing", "/careers/feed/")
_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
_JOB_URL_RE = re.compile(r"/job|/career|/vacanc|/position|/opening", re.I)

MAX_SITEMAP_PAGES = 30
_BAD_TOKENS = {"embed", "jobs", "job", "job_board", "embed_job_board"}


# ── HTTP helper ──────────────────────────────────────────────────────────────


def _looks_like_careers(html: str) -> bool:
    return bool(_CAREERS_ANCHOR_RE.search(html[:20000]))


# ── careers-URL discovery ────────────────────────────────────────────────────


def _candidate_careers_urls(domain: str) -> list[str]:
    out = [f"https://careers.{domain}", f"https://jobs.{domain}"]
    for root in (f"https://{domain}", f"https://www.{domain}"):
        out.extend(root + p for p in _CAREERS_PATHS)
    return out


def _careers_link_from_homepage(html: str, base: str) -> str | None:
    for href, text in _ANCHOR_RE.findall(html[:100000]):
        if _CAREERS_ANCHOR_RE.search(href) or _CAREERS_ANCHOR_RE.search(strip_html(text)):
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = urljoin(base, href)
            if absu.startswith("http"):
                return absu
    return None


def _fetch_careers_page(
    domain: str, client: httpx.Client, notes: list[str]
) -> tuple[str | None, str | None]:
    """Returns (html, url) of the careers page, or (None, None)."""
    for url in _candidate_careers_urls(domain)[:10]:
        r = _get(client, url)
        if r is not None and _looks_like_careers(r.text):
            return r.text, str(r.url)
        time.sleep(0.2)
    # homepage anchor scan
    for base in (f"https://{domain}", f"https://www.{domain}"):
        r = _get(client, base)
        if r is None:
            continue
        link = _careers_link_from_homepage(r.text, base)
        if link:
            r2 = _get(client, link)
            if r2 is not None and _looks_like_careers(r2.text):
                return r2.text, str(r2.url)
        time.sleep(0.2)
    return None, None


# ── board-link scan (ATS discovery) ──────────────────────────────────────────


def scan_board_links(html: str) -> dict[str, str]:
    """First plausible board token per provider found in page HTML."""
    tokens: dict[str, str] = {}
    for provider, pat in BOARD_PATTERNS.items():
        for m in pat.finditer(html):
            tok = m.group(1)
            if tok.lower() in _BAD_TOKENS:
                continue
            tokens[provider] = tok
            break
    return tokens


def _verify_tokens(
    tokens: dict[str, str], name: str, client: httpx.Client
) -> dict[str, str]:
    """Keep only tokens whose board responds with live postings."""
    confirmed: dict[str, str] = {}
    for provider, tok in tokens.items():
        try:
            jobs = FETCHERS[provider](
                tok, client, company=name, company_slug=slugify(name), content=False
            )
        except (SourceError, SourceNotFound):
            time.sleep(0.15)
            continue
        if jobs:
            confirmed[provider] = tok
    return confirmed


# ── JSON-LD JobPosting extraction ────────────────────────────────────────────


def extract_jobpostings(html: str) -> list[dict]:
    out: list[dict] = []
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        out.extend(_collect_jobpostings(data))
    return out


def _collect_jobpostings(node) -> list[dict]:
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() == "jobposting" for x in types):
            return [node]
        return [j for v in node.values() for j in _collect_jobpostings(v)]
    if isinstance(node, list):
        return [j for v in node for j in _collect_jobpostings(v)]
    return []


def _location_str(j: dict) -> str | None:
    jl = j.get("jobLocation")
    if isinstance(jl, list):
        jl = jl[0] if jl else None
    if not isinstance(jl, dict):
        return None
    pa = jl.get("address") or {}
    parts = [pa.get("addressLocality"), pa.get("addressRegion") or pa.get("addressCountry")]
    s = ", ".join(str(p) for p in parts if p)
    return s or None


def jsonld_to_posting(j: dict, page_url: str, company: str) -> RawPosting | None:
    title = str(j.get("title") or "").strip()
    if not title:
        return None
    url = str(j.get("url") or page_url)
    desc = strip_html(j.get("description") or "")[:4000] or None
    return RawPosting(
        source=f"careers:{urlparse(page_url).netloc}",
        company=company,
        company_slug=slugify(company),
        title=title,
        url=url,
        location=_location_str(j),
        description=desc,
        posted_at=str(j.get("datePosted")) if j.get("datePosted") else None,
        job_type=str(j.get("employmentType")) if j.get("employmentType") else None,
    )


# ── WordPress Job Manager feeds ──────────────────────────────────────────────


def parse_wp_feed(xml_text: str, company: str, source_url: str) -> list[RawPosting]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    netloc = urlparse(source_url).netloc
    out: list[RawPosting] = []
    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue
        fields = {
            ch.tag.split("}")[-1]: (ch.text or "").strip()
            for ch in item
            if ch.tag.split("}")[-1] in ("title", "link", "description", "pubDate")
        }
        title = fields.get("title", "")
        link = fields.get("link", "")
        if not title or not link:
            continue
        out.append(
            RawPosting(
                source=f"careers:{netloc}",
                company=company,
                company_slug=slugify(company),
                title=title,
                url=link,
                description=(strip_html(fields.get("description", ""))[:4000] or None),
                posted_at=fields.get("pubDate") or None,
            )
        )
    return out


# ── sitemap crawl ────────────────────────────────────────────────────────────


def _locs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "loc" and el.text:
            out.append(el.text.strip())
    return out


def _sitemap_job_urls(domain: str, client: httpx.Client) -> list[str]:
    urls: list[str] = []
    for root in (f"https://{domain}", f"https://www.{domain}"):
        for path in _SITEMAP_PATHS:
            r = _get(client, root + path)
            if r is None:
                continue
            locs = _locs(r.text)
            if not locs:
                continue
            # sitemap index → fetch a few children
            if "<sitemapindex" in r.text[:1000].lower():
                for child in locs[:5]:
                    rc = _get(client, child)
                    if rc is not None:
                        locs.extend(_locs(rc.text))
                    time.sleep(0.2)
            urls = [u for u in locs if u.startswith("http") and _JOB_URL_RE.search(u)]
            if urls:
                return urls
            time.sleep(0.2)
    return []


def _crawl_sitemap(
    domain: str, client: httpx.Client, company: str, notes: list[str]
) -> list[RawPosting]:
    urls = _sitemap_job_urls(domain, client)
    if not urls:
        return []
    postings: list[RawPosting] = []
    for u in urls[:MAX_SITEMAP_PAGES]:
        r = _get(client, u)
        if r is None:
            continue
        for j in extract_jobpostings(r.text):
            p = jsonld_to_posting(j, u, company)
            if p is not None:
                postings.append(p)
        time.sleep(0.3)
    notes.append(f"sitemap: {len(postings)} postings from {len(urls[:MAX_SITEMAP_PAGES])} urls")
    return postings


# ── orchestrator ─────────────────────────────────────────────────────────────


def crawl_company(domain: str | None, name: str, client: httpx.Client) -> dict:
    """Crawl one company's static careers presence.

    Returns {"career_url", "board_tokens" (verified), "postings", "notes"}.
    Board tokens beat page scraping: once discovered, the standard ATS
    connector owns future pulls and the watchlist self-heals (§5.9).
    """
    out: dict = {"career_url": None, "board_tokens": {}, "postings": [], "notes": []}
    if not domain:
        out["notes"].append("no domain — careers crawl skipped")
        return out

    html, fetched_url = _fetch_careers_page(domain, client, out["notes"])
    if html is None:
        out["notes"].append("no static careers page found (JS site? P7 sidecar)")
        return out
    out["career_url"] = fetched_url

    # 1. board links (careers page first, homepage as backup)
    tokens = scan_board_links(html)
    if not tokens:
        for base in (f"https://{domain}", f"https://www.{domain}"):
            r = _get(client, base)
            if r is not None:
                tokens = scan_board_links(r.text)
                if tokens:
                    break
            time.sleep(0.2)
    if tokens:
        confirmed = _verify_tokens(tokens, name, client)
        if confirmed:
            out["board_tokens"] = confirmed
            found = ", ".join(f"{p}:{t}" for p, t in confirmed.items())
            out["notes"].append(f"ATS board discovered via careers page: {found}")
            for provider, tok in confirmed.items():
                try:
                    out["postings"].extend(
                        FETCHERS[provider](
                            tok, client, company=name, company_slug=slugify(name)
                        )
                    )
                except (SourceError, SourceNotFound):
                    continue
            return out

    # 2. JSON-LD on the careers page
    ld = extract_jobpostings(html)
    for j in ld:
        p = jsonld_to_posting(j, fetched_url, name)
        if p is not None:
            out["postings"].append(p)
    if ld:
        out["notes"].append(f"json-ld: {len(ld)} postings")

    # 3. WP Job Manager feeds
    for feed_path in _WP_FEED_PATHS:
        feed_url = urljoin(fetched_url, feed_path)
        r = _get(client, feed_url)
        if r is not None and "<rss" in r.text[:600].lower():
            items = parse_wp_feed(r.text, name, feed_url)
            if items:
                out["postings"].extend(items)
                out["notes"].append(f"wp-feed: {len(items)} postings")
                break
        time.sleep(0.2)

    # 4. sitemap (only when the page itself gave nothing)
    if not out["postings"]:
        out["postings"].extend(_crawl_sitemap(domain, client, name, out["notes"]))

    if not out["postings"]:
        out["notes"].append("careers page found but no static postings (JS render? P7)")
    return out
