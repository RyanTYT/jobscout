"""webapp/ui.py — view-model layer for the dashboard.

Keeps routes.py thin: parsing raw query params into a typed filter object,
computing pagination windows, and mapping statuses to nav counts. Pure
functions/dataclasses only — trivially unit-testable without a server.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

from markupsafe import Markup

# allowed values — anything else from the query string is dropped
STATUSES = ("new", "interested", "dismissed", "applied", "withdrawn", "all")
TIERS = ("A", "B", "C")
SORTS = (
    "score", "score_asc", "newest", "oldest", "posted", "company",
    "company_desc", "title", "title_desc", "location",
    "location_desc", "level", "level_desc",
)
PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50

# pagination window around the current page (… gap beyond this)
_PAGE_NEIGHBORS = 2


@dataclass
class InboxFilters:
    """Every filter the inbox table understands, normalised."""

    status: str = "new"
    tier: str = ""
    q: str = ""
    min_score: str = ""
    all_postings: bool = False
    level: str = ""
    location: str = ""
    company: str = ""
    source: str = ""
    remote: str = ""
    sort: str = "score"

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_query(cls, **params: str) -> InboxFilters:
        """Build from raw request params, dropping unknown values."""
        f = cls()
        if params.get("status") in STATUSES:
            f.status = params["status"]
        if params.get("tier") in TIERS:
            f.tier = params["tier"]
        f.q = (params.get("q") or "").strip()[:120]
        f.min_score = (params.get("min_score") or "").strip()[:8]
        f.all_postings = params.get("all_postings") == "1"
        f.level = (params.get("level") or "").strip()[:40]
        f.location = (params.get("location") or "").strip()[:80]
        f.company = (params.get("company") or "").strip()[:80]
        f.source = (params.get("source") or "").strip()[:30]
        f.remote = "1" if params.get("remote") == "1" else ""
        if params.get("sort") in SORTS:
            f.sort = params["sort"]
        return f

    # ── queries ─────────────────────────────────────────────────────────────

    def as_kwargs(self) -> dict:
        """The dict passed to db.list_postings / db.count_postings."""
        kwargs: dict = {
            "status": self.status,
            "tier": self.tier or None,
            "q": self.q or None,
            "level": self.level or None,
            "location": self.location or None,
            "company": self.company or None,
            "source": self.source or None,
            "remote": self.remote or None,
            "only_rule_pass": not self.all_postings,
            "sort": self.sort,
        }
        try:
            kwargs["min_score"] = float(self.min_score) if self.min_score else None
        except ValueError:
            kwargs["min_score"] = None
        return kwargs

    def query_string(self, **overrides) -> str:
        """URL-encoded filter state (page etc. via overrides)."""
        parts: list[tuple[str, str]] = []
        data = replace(self, **overrides) if overrides else self
        if data.status and data.status != "new":
            parts.append(("status", data.status))
        if data.tier:
            parts.append(("tier", data.tier))
        if data.q:
            parts.append(("q", data.q))
        if data.min_score:
            parts.append(("min_score", data.min_score))
        if data.all_postings:
            parts.append(("all_postings", "1"))
        if data.level:
            parts.append(("level", data.level))
        if data.location:
            parts.append(("location", data.location))
        if data.company:
            parts.append(("company", data.company))
        if data.source:
            parts.append(("source", data.source))
        if data.remote:
            parts.append(("remote", data.remote))
        if data.sort and data.sort != "score":
            parts.append(("sort", data.sort))
        return urlencode(parts)

    def is_filtered(self) -> bool:
        """True when any non-default filter is active (drives 'reset')."""
        defaults = InboxFilters()
        return self.query_string() != defaults.query_string()


@dataclass
class Pagination:
    """Server-side pagination state for the inbox."""

    total: int
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    filters: InboxFilters = field(default_factory=InboxFilters)

    @classmethod
    def from_query(cls, total: int, page_raw: str, size_raw: str,
                   filters: InboxFilters) -> Pagination:
        try:
            page = max(1, int(page_raw or "1"))
        except ValueError:
            page = 1
        try:
            size = int(size_raw or str(DEFAULT_PAGE_SIZE))
        except ValueError:
            size = DEFAULT_PAGE_SIZE
        if size not in PAGE_SIZES:
            size = DEFAULT_PAGE_SIZE
        return cls(total=total, page=page, page_size=size, filters=filters)

    # ── derived ─────────────────────────────────────────────────────────────

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def clamped_page(self) -> int:
        return min(self.page, self.pages)

    @property
    def offset(self) -> int:
        return (self.clamped_page - 1) * self.page_size

    @property
    def start(self) -> int:
        return min(self.offset + 1, self.total)

    @property
    def end(self) -> int:
        return min(self.offset + self.page_size, self.total)

    @property
    def window(self) -> list[int | None]:
        """Page numbers to render, with None gaps: [1, 2, None, 8, 9, 10]."""
        last = self.pages
        cur = self.clamped_page
        if last <= _PAGE_NEIGHBORS * 2 + 5:
            return list(range(1, last + 1))
        lo = max(1, cur - _PAGE_NEIGHBORS)
        hi = min(last, cur + _PAGE_NEIGHBORS)
        items: list[int | None] = []
        if lo > 1:
            items.append(1)
            if lo > 2:
                items.append(None)
        items.extend(range(lo, hi + 1))
        if hi < last:
            if hi < last - 1:
                items.append(None)
            items.append(last)
        return items

    def qs_page(self, page: int) -> str:
        """Query string for a specific page, preserving all filters."""
        qs = self.filters.query_string()
        parts = [f"page={page}", f"page_size={self.page_size}"]
        if qs:
            parts.append(qs)
        return "&".join(parts)


def nav_counts(conn) -> dict:
    """Badge counts for the sidebar (per-status totals)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM postings WHERE rule_pass = 1 "
        "GROUP BY status"
    ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    packets = conn.execute(
        "SELECT COUNT(*) FROM packets WHERE status NOT IN ('applied', 'withdrawn')"
    ).fetchone()[0]
    return {
        "new": counts.get("new", 0),
        "interested": counts.get("interested", 0),
        "applied": counts.get("applied", 0),
        "packets": packets,
        "all": sum(counts.values()),
    }


# option lists consumed by the inbox filter panel (Jinja has no
# comprehensions — keep this logic in Python, out of templates)
STATUS_OPTIONS = [(s, s.title(), None) for s in STATUSES]
TIER_OPTIONS = [(t, f"tier {t}", None) for t in TIERS]
SORT_OPTIONS = [
    ("score", "score ↓", None), ("score_asc", "score ↑", None),
    ("newest", "newest first", None), ("oldest", "oldest first", None),
    ("posted", "posting date", None), ("company", "company A–Z", None),
    ("company_desc", "company Z–A", None), ("title", "title A–Z", None),
    ("title_desc", "title Z–A", None),
]
SIZE_OPTIONS = [(str(s), str(s), None) for s in PAGE_SIZES]

NAV_ITEMS = (
    # (key, path, label, icon, count_key)
    ("inbox", "/", "Inbox", "inbox", None),
    ("companies", "/companies", "Companies", "building", None),
    ("discovery", "/discovery", "Discovery", "radar", None),
    ("applications", "/applications", "Applications", "doc", "packets"),
    ("profile", "/profile", "Profile", "spark", None),
    ("ops", "/ops", "Ops", "pulse", None),
)


def template_ctx(version: str, active: str, counts: dict) -> dict:
    """Common context every template receives."""
    profile_missing = counts.pop("profile_missing", 0) if counts else 0
    return {
        "profile_missing": profile_missing,
        "version": version,
        "active": active,
        "nav_items": [
            {
                "key": key, "path": path, "label": label, "icon": icon,
                "count": counts.get(count_key) if count_key else None,
            }
            for key, path, label, icon, count_key in NAV_ITEMS
        ],
        "statuses": STATUSES,
        "tiers": TIERS,
        "sorts": SORTS,
        "page_sizes": PAGE_SIZES,
        "status_options": STATUS_OPTIONS,
        "tier_options": TIER_OPTIONS,
        "sort_options": SORT_OPTIONS,
        "size_options": SIZE_OPTIONS,
    }


# ── signal rendering helpers ─────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s)\"'<>,;]+")


def linkify(text: str):
    """Escape text, turning embedded URLs into highlighted chips.

    The non-URL parts are html-escaped here; templates render the result
    unescaped.
    """
    if not text:
        return Markup("")
    out = []
    pos = 0
    for m in _URL_RE.finditer(text):
        out.append(_html.escape(text[pos:m.start()]))
        url = m.group(0).rstrip(".!?")
        if url:
            shown = url.removeprefix("https://").removeprefix("http://")
            out.append(
                f'<a class="signal__url" href="{_html.escape(url)}" '
                f'target="_blank" rel="noopener">{_html.escape(shown)}</a>'
            )
        pos = m.end()
    out.append(_html.escape(text[pos:]))
    return Markup("".join(out))


# what each signal kind means — the legend on the companies page
SIGNAL_KINDS = {
    "news": "Google News match — a headline mentioning the company together "
            "with hiring/expansion vocabulary (funding, growth, layoffs). "
            "Earliest public signal that headcount is moving.",
    "sitemap_new_url": "A new job URL appeared in the company sitemap. "
                       "CMS generates the URL the moment a draft posting is "
                       "saved — often days before it goes live.",
    "careers_page_changed": "The careers page content hash changed since the "
                            "last daily check; the job-title diff shows what "
                            "was added or removed.",
    "github_activity": "A repo under the company's GitHub org was pushed "
                       "within the last 7 days — active engineering.",
    "hn": "Who-is-hiring thread comment by or about the company "
          "(monthly Hacker News thread).",
    "hn_mention": "Recent Hacker News comment mentioning the company in a "
                  "hiring context, outside the Who-is-hiring thread.",
    "funding": "RSS funding-feed match (TechCrunch etc.) — new money "
               "usually precedes new roles.",
    "ats_found": "A public ATS board token was discovered for a previously "
                 "dark-pool company (watchlist self-heal).",
}

_SIGNAL_ICON_HINTS = {
    "news": "news", "github_activity": "github", "funding": "bolt",
    "hn": "radar", "hn_mention": "radar", "careers_page_changed": "sitemap",
    "sitemap_new_url": "sitemap", "ats_found": "building",
}


def signal_kind_meta(kind: str) -> dict:
    """(label, explanation, icon) for a signal kind — used by templates."""
    return {
        "label": kind.replace("_", " "),
        "desc": SIGNAL_KINDS.get(kind, "signal emitted by a discovery source"),
        "icon": _SIGNAL_ICON_HINTS.get(kind, "radar"),
    }


# ── sortable inbox headers ───────────────────────────────────────────────────

# column -> (ascending sort key, descending sort key)
SORT_COLUMNS = {
    "score": ("score_asc", "score"),
    "title": ("title", "title_desc"),
    "company": ("company", "company_desc"),
    "location": ("location", "location_desc"),
    "level": ("level", "level_desc"),
    "first_seen": ("oldest", "newest"),
}


def sort_href(filters: InboxFilters, next_sort: str) -> str:
    """URL for the inbox with `sort` changed and page reset to 1."""
    qs = filters.query_string(sort=next_sort)
    parts = ["page=1"]
    if qs:
        parts.append(qs)
    return "/?" + "&".join(parts)


def inbox_sort_headers(filters: InboxFilters) -> list[dict]:
    """Header spec for the inbox table: label, href, direction, active."""
    specs = []
    columns = (
        ("score", "score"), ("title", "title"), ("company", "company"),
        ("location", "location"), ("level", "level"),
        ("source", None), ("first seen", "first_seen"),
    )
    for label, key in columns:
        if key is None:
            specs.append({"label": label, "href": None, "dir": None,
                          "active": False})
            continue
        asc, desc = SORT_COLUMNS[key]
        active = filters.sort in (asc, desc)
        nxt = desc if filters.sort == asc else asc
        specs.append({
            "label": label,
            "href": sort_href(filters, nxt),
            "dir": "asc" if filters.sort == asc
                   else "desc" if filters.sort == desc else None,
            "active": active,
        })
    return specs
