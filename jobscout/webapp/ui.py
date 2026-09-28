"""webapp/ui.py — view-model layer for the dashboard.

Keeps routes.py thin: parsing raw query params into a typed filter object,
computing pagination windows, and mapping statuses to nav counts. Pure
functions/dataclasses only — trivially unit-testable without a server.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

# allowed values — anything else from the query string is dropped
STATUSES = ("new", "interested", "dismissed", "applied", "withdrawn", "all")
TIERS = ("A", "B", "C")
SORTS = ("score", "newest", "posted", "company", "title")
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
    ("score", "score", None), ("newest", "newest", None),
    ("posted", "posted date", None), ("company", "company", None),
    ("title", "title", None),
]
SIZE_OPTIONS = [(str(s), str(s), None) for s in PAGE_SIZES]

NAV_ITEMS = (
    # (key, path, label, icon, count_key)
    ("inbox", "/", "Inbox", "inbox", None),
    ("companies", "/companies", "Companies", "building", None),
    ("discovery", "/discovery", "Discovery", "radar", None),
    ("applications", "/applications", "Applications", "doc", "packets"),
    ("ops", "/ops", "Ops", "pulse", None),
)


def template_ctx(version: str, active: str, counts: dict) -> dict:
    """Common context every template receives."""
    return {
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
