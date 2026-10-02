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
    monitoring: dict | None = None,
    discovery: dict | None = None,
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

    if discovery:
        lines.append("## Discovery")
        lines.append("")
        for c in (discovery.get("candidates") or [])[:12]:
            lines.append(f"- candidate: {_esc(c)}")
        for m in list(dict.fromkeys(discovery.get("matched") or []))[:12]:
            lines.append(f"- signal: {_esc(m)}")
        rss_s = discovery.get("rss") or {}
        if rss_s:
            if rss_s.get("error"):
                lines.append(f"- rss: error — {_esc(rss_s['error'])}")
            else:
                lines.append(
                    f"- rss: {rss_s.get('feeds', 0)} feeds / {rss_s.get('entries', 0)} entries / "
                    f"{rss_s.get('signals_new', 0)} new signals"
                )
                for u in (rss_s.get("unknown") or [])[:6]:
                    lines.append(f"- unresolved mention: {_esc(u)} (no domain — resolve via agent)")
        hn_s = discovery.get("hn") or {}
        if hn_s:
            if hn_s.get("error"):
                lines.append(f"- hn: error — {_esc(hn_s['error'])}")
            elif hn_s.get("skipped"):
                lines.append(f"- hn: {_esc(hn_s['skipped'])}")
            else:
                lines.append(
                    f"- hn: {_esc(hn_s.get('story'))} — {hn_s.get('comments_seen', 0)} comments, "
                    f"{hn_s.get('comments_matched', 0)} matched keywords"
                )
        cse_s = discovery.get("cse") or {}
        if cse_s:
            if cse_s.get("error"):
                lines.append(f"- cse: error — {_esc(cse_s['error'])}")
            elif cse_s.get("skipped"):
                lines.append(f"- cse: {_esc(cse_s['skipped'])}")
            else:
                lines.append(
                    f"- cse: {cse_s.get('queries', 0)} queries / {cse_s.get('results', 0)} results"
                )
        lines.append("")

    if monitoring:
        lines.append("## Monitoring")
        lines.append("")
        pc = monitoring.get("page_changes") or {}
        sm = monitoring.get("sitemap") or {}
        if pc.get("error"):
            lines.append(f"- page changes: error — {_esc(pc['error'])}")
        elif pc:
            lines.append(
                f"- careers pages: {pc.get('companies_checked', 0)} checked, "
                f"{pc.get('changes_detected', 0)} changed, "
                f"{pc.get('signals_new', 0)} new signals"
            )
        if sm.get("error"):
            lines.append(f"- sitemaps: error — {_esc(sm['error'])}")
        elif sm:
            lines.append(
                f"- sitemaps: {sm.get('companies_checked', 0)} checked, "
                f"{sm.get('new_urls', 0)} new job URLs"
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

    scored = run_meta.get("scored")
    scoring_line = ""
    if scored:
        cap_note = " · CAP HIT, rest deferred" if scored.get("capped") else ""
        scoring_line = f" · scored {scored.get('scored', 0)}{cap_note}"
    lines.append("---")
    lines.append(
        f"run {run_meta.get('run_id')} · {run_meta.get('companies', 0)} companies · "
        f"{run_meta.get('postings_seen', 0)} postings seen{scoring_line} · mode: pipeline"
    )

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── reading reports back ─────────────────────────────────────────────────────
# write_daily emits markdown; these read it back into structured data. Restored
# 2026-10-02 from surviving CPython 3.13 bytecode (the originals were uncommitted
# and lost to a `git filter-repo --force` reset), then re-verified against that
# bytecode: list_reports and parse_daily_report compile to an identical
# instruction stream, parse_morning_report differs only by one redundant
# JUMP_BACKWARD, and all three run identically over the real reports on disk.
#
# NOTE: the webapp report browser does NOT use these. It reads the database via
# jobscout/webapp/reports.py, whose promotion-table "note" is the human-entered
# companies.notes value. The "note" parsed here is the watchlist-coverage table's
# per-company ATS note written by write_daily — a different thing. Don't swap
# one for the other.


def list_reports(kind: str, retention_days: int = 30) -> list[dict]:
    """Return list of available report files, newest first.
    kind: 'daily' -> digest/ dir, 'morning' -> brief/ dir
    Deletes files older than retention_days."""
    from datetime import UTC, datetime, timedelta

    from jobscout.core.paths import digest_dir, morning_reports_dir

    base = digest_dir() if kind == "daily" else morning_reports_dir()
    if not base.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    files = sorted(base.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files:
        # retention sweep — old reports get reaped on read
        if datetime.fromtimestamp(f.stat().st_mtime, tz=UTC) < cutoff:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        stat = f.stat()
        out.append(
            {
                "date": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": kind,
            }
        )
    return out


def parse_daily_report(path: Path) -> dict:
    """Parse a daily digest markdown into structured data for the viewer."""
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {
            "date": path.stem,
            "kind": "daily",
            "new_postings": [],
            "companies": [],
            "errors": [],
            "discovery": {},
            "monitoring": {},
            "raw": "",
        }

    lines = text.splitlines()

    data = {
        "date": path.stem,
        "kind": "daily",
        "new_postings": [],
        "companies": [],
        "candidates": [],
        "errors": [],
        "discovery": {},
        "monitoring": {},
        "raw": text,
    }

    section = None
    for line in lines:
        if line.startswith("## New today"):
            section = "new"
            continue
        if line.startswith("## "):
            section = line[3:].lower()
            continue

        if (
            section == "new"
            and line.startswith("|")
            and not line.startswith("| #")
            and not line.startswith("|---")
        ):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 5:
                title_cell = parts[1]
                url_match = re.search(r"\]\(([^)]+)\)", title_cell)
                title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title_cell)
                data["new_postings"].append(
                    {
                        "title": title,
                        "url": url_match.group(1) if url_match else "",
                        "company": parts[2],
                        "location": parts[3],
                        "seniority": parts[4],
                        "source": parts[5] if len(parts) > 5 else "",
                    }
                )
                continue
            continue

        if (
            section == "watchlist coverage"
            and line.startswith("|")
            and not line.startswith("| company")
            and not line.startswith("|---")
        ):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 6:
                data["companies"].append(
                    {
                        "name": parts[0],
                        "tier": parts[1],
                        "ats": parts[2],
                        "jobs": parts[3],
                        "new": parts[4],
                        "note": parts[5],
                    }
                )
                continue
            continue

        if section == "discovery" and line.startswith("- candidate:"):
            name = line[len("- candidate:") :].strip()
            if name:
                data["candidates"].append({"name": name, "found_via": "discovery"})
                continue
            continue

        # write_daily emits "## Source errors (N)" — the count is part of the
        # heading, so match the prefix rather than the whole section key.
        if (section or "").startswith("source errors") and line.startswith("- "):
            data["errors"].append(line[2:])

    return data


def parse_morning_report(path: Path) -> dict:
    """Parse a morning brief markdown into structured data for the viewer."""
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {
            "date": path.stem,
            "kind": "morning",
            "companies_added": [],
            "signals_added": [],
            "notes_written": [],
            "final_summary": "",
            "db_changes": [],
            "raw": "",
        }

    lines = text.splitlines()

    data = {
        "date": path.stem,
        "kind": "morning",
        "companies_added": [],
        "signals_added": [],
        "notes_written": [],
        "final_summary": "",
        "db_changes": [],
        "raw": text,
    }

    section = None
    for line in lines:
        if line.startswith("## Tool log"):
            section = "tools"
            continue
        if line.startswith("## "):
            section = line[3:].lower()
            continue
        if section == "tools":

            if line.startswith("### step"):
                tool_match = re.search(r"`([^`]+)`", line)
                if tool_match:
                    data["_current_tool"] = tool_match.group(1)
                    continue
                continue
            if (
                line.startswith("- args:")
                and data.get("_current_tool") == "add_company"
            ):

                args_match = re.search(r"\{.*\}", line)
                if args_match:
                    try:
                        import json as _json

                        args = _json.loads(args_match.group(0))
                        data["companies_added"].append(args)
                    except Exception:
                        continue
                    continue
                continue
            if line.startswith("- args:") and data.get("_current_tool") == "add_signal":
                args_match = re.search(r"\{.*\}", line)
                if args_match:
                    try:
                        import json as _json

                        args = _json.loads(args_match.group(0))
                        data["signals_added"].append(args)
                    except Exception:
                        continue
                    continue
                continue
            if line.startswith("- result:") and data.get("_current_tool") == "add_company":
                # result echo — the - args: line above already captured it
                continue
            continue
        # harness writes "## DB changes (auditable diff)" when the run changed
        # something and a bare "## DB changes" when it did not — match the prefix.
        if (section or "").startswith("db changes") and line.startswith("- "):
            entry = line[2:].strip()
            # the writer emits a literal "- none" when nothing changed
            if entry and entry != "none":
                data["db_changes"].append(entry)
            continue
        if section == "final summary":
            data["final_summary"] += line + "\n"

    data.pop("_current_tool", None)
    return data
