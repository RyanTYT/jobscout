"""Daily markdown digest writer (PLAN §4 step 1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobscout.core.paths import digest_dir


def _esc(s: str | None) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def write_daily(
    digest_date: str,
    *,
    coverage: list[dict],
    new: list[sqlite3.Row],
    excluded_count: int,
    errors: list[str],
    closing: list[sqlite3.Row],
    run_meta: dict,
) -> Path:
    p = digest_dir() / f"{digest_date}.md"
    lines: list[str] = [f"# jobscout digest — {digest_date}", ""]

    lines.append(
        f"**{len(new)} new postings passed rules** ({excluded_count} excluded by rules). "
        "LLM fit scoring lands in P2 — this list is rule-filtered only."
    )
    lines.append("")

    if new:
        lines.append("## New today — rule pass")
        lines.append("")
        lines.append("| # | title | company | location | level | source |")
        lines.append("|---|-------|---------|----------|-------|--------|")
        for i, row in enumerate(new[:60], 1):
            title = _esc(row["title"])
            link = f"[{title}]({row['url']})"
            src = _esc(row["source"]).replace("ats:", "").replace(":", "/")
            lines.append(
                f"| {i} | {link} | {_esc(row['company_id'])} | "
                f"{_esc(row['location'])[:32]} | {_esc(row['seniority'])} | {src} |"
            )
        if len(new) > 60:
            lines.append(f"| … | {len(new) - 60} more (dashboard lands in P2) | | | | |")
        lines.append("")

    if closing:
        lines.append(f"## Closing / stale ({len(closing)}) — not seen for > 14 days")
        lines.append("")
        lines.append("| title | company | last seen |")
        lines.append("|-------|---------|-----------|")
        for row in closing[:15]:
            lines.append(
                f"| {_esc(row['title'])} | {_esc(row['company_id'])} | {row['last_seen'][:10]} |"
            )
        lines.append("")

    lines.append("## Watchlist coverage")
    lines.append("")
    lines.append("| company | tier | ATS boards | jobs | new | note |")
    lines.append("|---------|------|------------|------|-----|------|")
    for c in coverage:
        ats = ", ".join(f"{k}:{v}" for k, v in (c.get("ats") or {}).items())
        if not ats:
            ats = "none found (dark-pool entry)"
        lines.append(
            f"| {_esc(c['name'])} | {c['tier']} | {ats} | {c['jobs']} | {c['new']} | {_esc(c.get('note'))} |"
        )
    lines.append("")

    if errors:
        lines.append(f"## Source errors ({len(errors)})")
        lines.append("")
        for e in errors[:20]:
            lines.append(f"- {_esc(e)}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"run {run_meta.get('run_id')} · {run_meta.get('companies', 0)} companies · "
        f"{run_meta.get('postings_seen', 0)} postings seen · cost $0.00 · mode: pipeline (no LLM yet)"
    )

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
