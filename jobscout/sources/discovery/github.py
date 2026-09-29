"""GitHub org activity monitoring — free, no key for public orgs (rate-limited).

For each watchlist company, find their GitHub org (guessed from the domain
root, cached in state). If the org exists, check for recently pushed repos
(7 days). Active engineering = building = likely hiring.

Rate limits: 60 requests/hour without auth — fine for ~40 calls per sweep.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx

from jobscout.core import db
from jobscout.sources.postings.base import soft_get

_API = "https://api.github.com"

_RECENT_DAYS = 7


def _guess_orgs(domain: str) -> list[str]:
    """Guess GitHub org names from a domain root."""
    root = (domain or "").lower().removeprefix("www.").split(".")[0]
    if not root:
        return []
    return [root, f"{root}-io", f"{root}hq", f"{root}hq-labs"]


def _find_org(client: httpx.Client, name: str) -> str | None:
    """Check if a GitHub org exists (returns login name or None)."""
    r = soft_get(client, f"{_API}/orgs/{name}")
    if r is None:
        return None
    try:
        data = r.json()
        return data.get("login") if data.get("type") == "Organization" else None
    except ValueError:
        return None


def sweep(client: httpx.Client, conn: sqlite3.Connection, wl) -> dict:
    """Check watchlist companies for recent GitHub activity.

    Emits `github_activity` signals for orgs with recently pushed repos.
    Caches the found org name in state (github_org:{company_id}).
    """
    stats = {
        "companies_checked": 0, "orgs_found": 0, "repos_pushed": 0,
        "signals_new": 0, "matched": [],
    }
    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(days=_RECENT_DAYS)

    entries = [
        (e.name, e.domain)
        for tier in ("A", "B", "C", "candidates")
        for e in getattr(wl, tier) if e.domain
    ]
    # also check dark-pool companies from the DB (not in watchlist.yaml
    # tiers — they might be candidates)
    rows = conn.execute(
        "SELECT id, name, domain FROM companies WHERE domain IS NOT NULL"
    ).fetchall()
    for r in rows:
        if not any(name == r["name"] for name, _ in entries):
            entries.append((r["name"], r["domain"]))

    for name, domain in entries:
        cid = db.slugify(name)
        state_key = f"github_org:{cid}"
        cached_org = db.get_state(conn, state_key)

        if cached_org == "__none__":
            continue  # already checked, no org exists — skip
        org = cached_org
        if org is None:
            # guess and cache
            for guess in _guess_orgs(domain):
                found = _find_org(client, guess)
                if found:
                    org = found
                    break
                time.sleep(0.3)
            db.set_state(conn, state_key, org or "__none__")
        if org is None or org == "__none__":
            continue

        stats["companies_checked"] += 1
        stats["orgs_found"] += 1

        # check recent repos (sorted by pushed date)
        r = soft_get(client, f"{_API}/orgs/{org}/repos",
                     params={"sort": "pushed", "per_page": 10})
        if r is None:
            continue
        try:
            repos = r.json()
        except ValueError:
            continue
        if not isinstance(repos, list):
            continue

        for repo in repos:
            pushed = repo.get("pushed_at")
            if not pushed:
                continue
            try:
                pushed_dt = datetime.fromisoformat(
                    pushed.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if pushed_dt < recent_cutoff:
                continue  # not recent enough

            repo_name = repo.get("name", "?")
            is_new = db.upsert_signal(
                conn, kind="github_activity",
                key=f"{org}/{repo_name}:{pushed[:10]}",
                company_id=cid,
                note=f"{org}/{repo_name} pushed {pushed[:10]} — "
                     f"active engineering ({repo.get('language', '?')})",
            )
            if is_new:
                stats["signals_new"] += 1
                stats["matched"].append(
                    f"{name} (github): {org}/{repo_name} pushed"
                )
            stats["repos_pushed"] += 1

        time.sleep(0.5)  # politeness per company

    return stats
