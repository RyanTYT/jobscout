"""jobscout CLI — every capability is a verb (PLAN Appendix B).

P0: doctor, db init|status, config check|show, resume validate|fields
P1: run [--daily] [--force], add-company, probe, digest
Later-phase verbs exist as stubs pointing at the PLAN so the surface is discoverable.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobscout import __version__
from jobscout.core import db
from jobscout.core.config import (
    ConfigError,
    load_env,
    load_models_cfg,
    load_profile,
    load_settings,
    load_watchlist,
)
from jobscout.core.paths import digest_dir
from jobscout.core.resume import (
    ResumeError,
    field_map,
    validate_resume,
)

app = typer.Typer(no_args_is_help=True, help="jobscout — daily AI job-hunt pipeline")
console = Console()

db_app = typer.Typer(no_args_is_help=True, help="SQLite state")
config_app = typer.Typer(no_args_is_help=True, help="Configuration")
resume_app = typer.Typer(no_args_is_help=True, help="Master resume")
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(resume_app, name="resume")


# ── top-level ────────────────────────────────────────────────────────────────


@app.command()
def init_home() -> None:
    """Bootstrap a relocated runtime (JOBSCOUT_HOME, packaged desktop app).

    Seeds config/settings.yaml + master_resume/resume.yaml from the
    bundled defaults (snapshot taken at build time) when missing, then
    initialises the database. Idempotent — safe on every start.
    """
    import shutil

    from jobscout.core.db import init_db
    from jobscout.core.paths import config_dir, master_resume_dir, repo_root

    # seed source: the bundled snapshot (frozen apps; the release build
    # copies live config/ into jobscout/defaults/), falling back to the
    # repo's own config/ in dev — no second copy of config to keep in sync
    bundled = Path(__file__).resolve().parent / "defaults"
    if bundled.is_dir():
        seed_dir = bundled
        resume_seed = bundled / "resume.yaml"
    else:
        # dev: the package sits inside the repo checkout (repo_root() is
        # JOBSCOUT_HOME-relocated, i.e. the destination — not the source)
        repo = Path(__file__).resolve().parents[1]
        seed_dir = repo / "config"
        resume_seed = repo / "master_resume" / "resume.yaml"
    seeds = [
        (seed_dir / "settings.yaml", config_dir() / "settings.yaml"),
        (seed_dir / "profile.yaml", config_dir() / "profile.yaml"),
        (seed_dir / "models.yaml", config_dir() / "models.yaml"),
        (seed_dir / "watchlist.yaml", config_dir() / "watchlist.yaml"),
        (resume_seed, master_resume_dir() / "resume.yaml"),
    ]
    for src, dst in seeds:
        if not dst.is_file():
            if not src.is_file():
                console.print(f"[red]default missing: {src}[/]")
                raise typer.Exit(1)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            console.print(f"  seeded [green]{dst}[/] from defaults")
        else:
            console.print(f"  keep existing [dim]{dst}[/]")
    init_db()
    console.print(f"  home ready: [bold]{repo_root()}[/]")


@app.command()
def doctor() -> None:
    """Health-check the whole stack. Exit 1 on any failure."""
    from jobscout.doctor import run_checks

    checks = run_checks()
    table = Table(title="jobscout doctor", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    style = {"ok": "green", "warn": "yellow", "fail": "red"}
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    for c in checks:
        table.add_row(c.name, f"[{style[c.status]}]{icon[c.status]} {c.status}[/]", c.detail)
    console.print(table)
    failures = [c for c in checks if c.status == "fail"]
    if failures:
        raise typer.Exit(1)
    warns = [c for c in checks if c.status == "warn"]
    console.print(
        f"[green]{len(checks) - len(warns) - len(failures)} ok[/] · "
        f"[yellow]{len(warns)} warn[/] · [red]{len(failures)} fail[/]"
    )


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"jobscout {__version__}")


def _stub(verb: str, phase: str, note: str) -> None:
    console.print(
        f"[yellow]`jobscout {verb}` is not implemented yet — lands in {phase} (see PLAN.md §10).[/]"
    )
    if note:
        console.print(f"[dim]{note}[/]")
    raise typer.Exit(2)


@app.command()
def run(
    daily: bool = typer.Option(False, "--daily", help="Run as the scheduled daily pipeline"),
    force: bool = typer.Option(False, "--force", help="Ignore the freshness idempotency check"),
) -> None:
    """Pull watchlist ATS boards → dedup → rule filter → markdown digest (P1)."""
    from jobscout.run import run_daily

    raise typer.Exit(run_daily(force=force or not daily))


@app.command()
def add_company(
    name: str = typer.Argument(..., help="Company display name"),
    domain: str | None = typer.Option(None, "--domain", help="e.g. janestreet.com"),
    tier: str = typer.Option(
        "C", help="A | B | C | candidate (discovery default: candidate)"
    ),
    note: str | None = typer.Option(None, "--note"),
    no_probe: bool = typer.Option(False, "--no-probe", help="Skip ATS board probing"),
) -> None:
    """Add a company to the watchlist (ATS boards probed automatically, P1)."""
    from jobscout import watchlist as wlmod
    from jobscout.core.schema import WatchlistEntry
    from jobscout.sources.postings.base import make_client
    from jobscout.sources.postings.probe import probe_company

    if tier not in ("A", "B", "C", "candidate"):
        console.print(f"[red]tier must be A, B, C, or candidate — got {tier!r}[/]")
        raise typer.Exit(2)

    tokens: dict[str, str] = {}
    jobs: dict[str, int] = {}
    if not no_probe and domain:
        console.print(f"probing ATS boards for [bold]{name}[/] ({domain}) …")
        client = make_client()
        try:
            res = probe_company(name, domain, client=client)
        finally:
            client.close()
        tokens, jobs = res["tokens"], res["jobs"]
        if tokens:
            for provider, tok in tokens.items():
                console.print(f"  [green]✓[/] {provider}: {tok} ({jobs.get(provider, 0)} live jobs)")
        else:
            console.print(
                "  [yellow]no public ATS board found[/] — dark-pool entry "
                "(careers crawl lands in P3)"
            )

    w = wlmod.load()
    entry = WatchlistEntry(
        name=name,
        domain=domain,
        ats=tokens,
        note=note
        or ("" if tokens else "no public ATS board — dark-pool entry"),
    )
    if wlmod.add(w, entry, tier):
        wlmod.save(w)
        db.init_db()
        conn = db.connect()
        try:
            db.upsert_company(
                conn,
                name=name,
                domain=domain,
                tier=tier,
                ats_tokens=tokens,
                notes=entry.note,
            )
        finally:
            conn.close()
        console.print(f"[green]✓[/] {name} → tier {tier} (watchlist.yaml + db updated)")
    else:
        existing = wlmod.find(w, name)
        console.print(
            f"[yellow]{name} already on the watchlist[/]"
            + (f" (tier {existing[0]})" if existing else "")
            + " — use add-company with a new tier to move it, or edit config/watchlist.yaml"
        )
        raise typer.Exit(1)


@app.command()
def probe(
    name: str = typer.Argument(...),
    domain: str | None = typer.Option(None, "--domain"),
    no_careers: bool = typer.Option(False, "--no-careers", help="Skip careers-page discovery"),
) -> None:
    """Probe ATS boards + careers page for a company (the full ATS-absence probe, P3)."""
    from jobscout.sources import careers_page
    from jobscout.sources.postings.base import make_client
    from jobscout.sources.postings.probe import probe_company

    client = make_client()
    try:
        res = probe_company(name, domain, client=client)
        if res["tokens"]:
            table = Table(title=f"ATS boards — {name}")
            table.add_column("provider")
            table.add_column("token")
            table.add_column("live jobs", justify="right")
            for provider, tok in res["tokens"].items():
                table.add_row(provider, tok, str(res["jobs"].get(provider, 0)))
            console.print(table)
        else:
            console.print(f"[yellow]no ATS board by token guessing for {name}[/]")

        if domain and not res["tokens"] and not no_careers:
            console.print(f"crawling careers presence for [bold]{domain}[/] …")
            crawl = careers_page.crawl_company(domain, name, client)
            if crawl["career_url"]:
                console.print(f"  careers page: [link={crawl['career_url']}]{crawl['career_url']}[/link]")
            if crawl["board_tokens"]:
                for prov, tok in crawl["board_tokens"].items():
                    console.print(f"  [green]✓ board via careers page[/] {prov}: {tok}")
            console.print(f"  static postings found: {len(crawl['postings'])}")
            for note in crawl["notes"]:
                console.print(f"  [dim]· {note}[/]")
            if not crawl["career_url"]:
                console.print("  [yellow]no static careers page — dark-pool entry (P7 sidecar render later)[/]")
    finally:
        client.close()


@app.command()
def digest(today: bool = typer.Option(True, "--today", help="Show the latest digest")) -> None:
    """Print the most recent daily digest."""
    files = sorted(digest_dir().glob("*.md"))
    if not files:
        console.print("[yellow]no digests yet — run `jobscout run --daily`[/]")
        raise typer.Exit(1)
    latest: Path = files[-1]
    console.print(f"[bold]{latest}[/]")
    console.print(latest.read_text(encoding="utf-8"))


@app.command()
def agent(
    morning: bool = typer.Option(False, "--morning", help="Run the full Morning Brief (deterministic sweep + agent harness)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the agent harness with a scripted fake model (no key needed)"),
) -> None:
    """The Morning Brief: deterministic sweep, then the agent tool loop (P5).

    Without an API key the deterministic sweep still runs (free), and the
    harness is skipped — unless --dry-run exercises the loop with a fake model.
    """
    import os

    from jobscout import watchlist as wlmod
    from jobscout.core import db as dbmod
    from jobscout.core.config import load_env, load_profile, load_settings
    from jobscout.sources import discovery
    from jobscout.sources.postings.base import make_client

    settings = load_settings()
    if settings.discovery.mode == "off":
        console.print("[yellow]discovery.mode == off[/] — nothing to do (config/settings.yaml)")
        return
    profile = load_profile()
    wl = wlmod.load()
    dbmod.init_db()
    conn = dbmod.connect()
    client = make_client()
    env = {**os.environ, **load_env()}
    try:
        # 1. deterministic sweep (free)
        stats = discovery.run_sweep(client, conn, wl, profile, settings, env)
        if stats.get("watchlist_changed"):
            wlmod.save(wl)
        for c in (stats.get("candidates") or []):
            console.print(f"  [green]+ candidate[/] {c}")
        for m in list(dict.fromkeys(stats.get("matched") or []))[:8]:
            console.print(f"  [cyan]· signal[/] {m}")
        for key in ("rss", "hn", "cse"):
            part = stats.get(key) or {}
            if part.get("skipped"):
                console.print(f"  [yellow]{key}: skipped[/] — {part['skipped']}")
            elif part.get("error"):
                console.print(f"  [red]{key}: error[/] — {part['error']}")

        # 2. agent harness (P5)
        from jobscout.agent.harness import FakeAgentModel, run_morning
        from jobscout.llm import LlmClient

        if dry_run:
            console.print("[bold]agent harness (dry run — scripted fake model)[/]")
            result = run_morning(
                FakeAgentModel(), conn, wl, profile, settings, env, client
            )
            _print_agent_result(result)
            return
        llm = LlmClient(conn=conn, settings=settings, env=env)
        if llm.available:
            console.print("[bold]agent harness — Morning Brief[/]")
            result = run_morning(llm, conn, wl, profile, settings, env, client)
            if result.get("watchlist_changed"):
                wlmod.save(wl)
                console.print("[green]watchlist changed — saved[/]")
            _print_agent_result(result)
        else:
            console.print(
                "[yellow]agent harness skipped (no API key)[/] — deterministic sweep only. "
                "Set JOBSCOUT_LLM_API_KEY in .env, or try --dry-run to see the loop."
            )
    finally:
        client.close()
        conn.close()


def _print_agent_result(result: dict) -> None:
    console.print(
        f"  {result['steps']} steps · ${result['cost']:.4f}"
        + (f" · [yellow]{result['cap_note']}[/]" if result.get("cap_note") else "")
    )
    for d in (result.get("diff") or []):
        console.print(f"  [cyan]· change[/] {d}")
    console.print(result.get("final", "")[:800])
    console.print(f"  report → {result['report']}")


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """Serve the local dashboard (P2) — 127.0.0.1:8787 by default."""
    import uvicorn

    from jobscout.core.config import load_settings as _ls
    from jobscout.webapp import create_app

    settings = _ls()
    app_asgi = create_app()
    uvicorn.run(
        app_asgi,
        host=host or settings.dashboard.host,
        port=port or settings.dashboard.port,
        log_level="info",
    )


@app.command()
def score(limit: int = typer.Option(500, "--limit", help="Max postings to score this invocation")) -> None:
    """Bulk-score rule-pass postings (tier A first, cap-respecting, cached)."""
    from jobscout.core.config import load_profile as _lp
    from jobscout.llm import LlmClient
    from jobscout.scoring import llm_bulk

    profile = _lp()
    llm = LlmClient()
    if not llm.available:
        console.print("[red]no API key — set JOBSCOUT_LLM_API_KEY in .env[/]")
        raise typer.Exit(1)
    db.init_db()
    conn = db.connect()
    try:
        rows = db.unscored_rule_pass(conn, limit=limit)
        if not rows:
            console.print("[green]nothing to score[/] — all eligible postings have final scores")
            raise typer.Exit(0)
        console.print(f"scoring {len(rows)} postings (tier {rows[0]['tier'] or '?'} first) …")
        stats = llm_bulk.score_postings(conn, rows, profile, llm)
    finally:
        conn.close()
    console.print(
        f"[green]✓[/] scored {stats['scored']} ({stats['errors']} errors"
        + (", [yellow]CAP HIT[/]" if stats["capped"] else "")
        + ") · tokens metered in llm_calls (see `jobscout stats`)"
    )


@app.command()
def stats() -> None:
    """DB counts, LLM spend by tier, recent runs."""
    status = db.db_status()
    if not status["exists"]:
        console.print("[yellow]database not initialised — run `jobscout db init`[/]")
        raise typer.Exit(1)
    conn = db.connect()
    try:
        t1 = Table(title="state")
        t1.add_column("table")
        t1.add_column("rows", justify="right")
        for k, v in status["counts"].items():
            t1.add_row(k, str(v))
        console.print(t1)

        t2 = Table(title="LLM spend (7 days)")
        t2.add_column("tier")
        t2.add_column("model")
        t2.add_column("calls", justify="right")
        t2.add_column("in tok", justify="right")
        t2.add_column("out tok", justify="right")
        t2.add_column("cost $", justify="right")
        for r in db.llm_spend_by_tier(conn):
            t2.add_row(
                r["tier"], r["model"], str(r["calls"]),
                f"{r['ptok'] or 0:,}", f"{r['ctok'] or 0:,}",
                f"{(r['cost'] or 0):.4f}",
            )
        console.print(t2)

        t3 = Table(title="recent runs")
        t3.add_column("id", justify="right")
        t3.add_column("kind")
        t3.add_column("started")
        t3.add_column("ok")
        t3.add_column("cost $", justify="right")
        for r in db.recent_runs(conn):
            t3.add_row(str(r["id"]), r["kind"], (r["started"] or "")[:19],
                       "✓" if r["ok"] else "✗", f"{(r['cost_usd'] or 0):.4f}")
        console.print(t3)
    finally:
        conn.close()


@app.command()
def prepare(
    posting: str = typer.Option(..., "--posting", help="posting id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use scripted fake models (no key needed)"),
    force: bool = typer.Option(False, "--force", help="Regenerate even if a packet exists"),
) -> None:
    """Generate an application packet for a selected posting (P6)."""
    from jobscout.packets.orchestrator import PacketError, prepare_packet

    db.init_db()
    conn = db.connect()
    try:
        if not force and not dry_run:
            existing = db.get_packet_for_posting(conn, posting)
            if existing is not None:
                console.print(
                    f"[yellow]packet already exists[/] ({existing['id']}, status "
                    f"{existing['status']}) — use --force to regenerate, or see "
                    f"{existing['dir']}"
                )
                raise typer.Exit(0)
        result = prepare_packet(conn, posting, dry_run=dry_run, force=force)
    except PacketError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    finally:
        conn.close()
    color = "green" if result["status"] == "ready" else "yellow"
    console.print(f"[{color}]packet {result['status']}[/] · {result['packet_id']}")
    console.print(f"  dir: {result['dir']}")
    console.print(f"  cost: ${result['cost']:.4f}"
                  + (f" · unsupported claims: {result['unsupported']}" if result["unsupported"] else ""))
    for r in result["reasons"]:
        console.print(f"  [yellow]![/] {r}")


@app.command()
def mark(vid: str, status: str = typer.Argument(...)) -> None:
    """Record an outcome: applied | dismissed | withdrawn (P6)."""
    db.init_db()
    conn = db.connect()
    try:
        if not db.set_posting_status(conn, vid, status):
            console.print(
                f"[red]could not mark {vid} as {status!r}[/] "
                "(unknown posting or status must be applied|dismissed|withdrawn)"
            )
            raise typer.Exit(1)
        if status == "applied":
            packet = db.get_packet_for_posting(conn, vid)
            if packet is not None:
                db.set_packet_status(conn, packet["id"], "applied")
        console.print(f"[green]✓[/] {vid} → {status}")
    finally:
        conn.close()


@app.command()
def scan_form(
    posting_id: str = typer.Argument(..., help="posting id"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Scan the real ATS application form for a posting (read-only, P7).

    Spawns the JobPilot sidecar, navigates to the posting's apply URL,
    and collects all labelled form fields. Writes fill_sheet_scanned.yaml
    into the packet directory.
    """
    import yaml

    from jobscout.sidecar import SidecarClient, SidecarError

    db.init_db()
    conn = db.connect()
    try:
        posting = db.get_posting(conn, posting_id)
        if posting is None:
            console.print(f"[red]no such posting: {posting_id}[/]")
            raise typer.Exit(1)
        packet = db.get_packet_for_posting(conn, posting_id)
    finally:
        conn.close()

    url = posting["url"]
    console.print(f"scanning form at [bold]{url}[/] …")
    try:
        with SidecarClient(headless=headless) as sidecar:
            result = sidecar.scan_form(url, timeout=45)
    except SidecarError as e:
        console.print(f"[red]sidecar error: {e}[/]")
        console.print("  build: cd ../JobPilot/scraper && npm run build-internal")
        raise typer.Exit(1) from e

    fields = result.get("fields") or []
    console.print(f"  [green]{len(fields)} fields[/] found")
    for f in fields[:20]:
        req = " *" if f.get("required") else ""
        opts = f" ({', '.join(f.get('options', [])[:3])}…)" if f.get("options") else ""
        console.print(f"  · {f['label']}{req} [{f['kind']}]{opts}")
    if len(fields) > 20:
        console.print(f"  … +{len(fields) - 20} more")

    # write into the packet directory if a packet exists
    if packet and packet["dir"]:
        out = Path(packet["dir"]) / "fill_sheet_scanned.yaml"
        out.write_text(yaml.safe_dump({"source": "scanned", "url": url,
                                        "fields": fields},
                                       sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
        console.print(f"  written: {out}")
    else:
        console.print("  (no packet yet — run `jobscout prepare --posting "
                       f"{posting_id}` first to store the scan in a packet)")


@app.command()
def fill(
    posting_id: str = typer.Argument(..., help="posting id"),
) -> None:
    """[Fill for me] — headful sidecar fills the form; you press submit (P7).

    Spawns the JobPilot sidecar in HEADED mode with pauseOnUncertainty=true,
    navigates to the posting's apply URL, and fills every field using the
    profile from the master resume. The browser window opens on YOUR screen —
    you watch the fill happen and press the submit button yourself.
    """
    import json

    from jobscout.core.resume import ResumeError, field_map, load_master_resume
    from jobscout.sidecar import SidecarClient, SidecarError

    db.init_db()
    conn = db.connect()
    try:
        posting = db.get_posting(conn, posting_id)
        if posting is None:
            console.print(f"[red]no such posting: {posting_id}[/]")
            raise typer.Exit(1)
    finally:
        conn.close()

    # load profile from master resume → UserProfile shape
    try:
        resume = load_master_resume()
    except ResumeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e

    fm_entries = field_map(resume)
    profile = {e.canonical_key: (e.value if e.value is not None else "") for e in fm_entries}
    # add the extra fields the fillers expect
    profile.setdefault("firstName", resume.identity.full_name or "")
    profile.setdefault("lastName", "")
    profile.setdefault("resumePath", "")

    url = posting["url"]
    console.print(f"[bold]Fill for me[/] — opening [link={url}]{url}[/link] in a browser …")
    console.print("[yellow]the browser window will open on your screen; watch the fill "
                  "and press submit yourself when it pauses[/]")

    # headful mode (visible browser) with pauseOnUncertainty
    job = {
        "id": posting_id,
        "title": posting["title"],
        "company": posting["company_name"] or posting["company_id"],
        "applyUrl": url,
        "applyHostname": url.split("/")[2] if "://" in url else "",
    }
    settings = {"pauseOnUncertainty": True, "maxConcurrentApplications": 1}

    try:
        with SidecarClient(headless=False, max_workers=1) as sidecar:
            resp = sidecar.request(
                "applyJobsByPayload",
                {"jobs": [job], "profile": profile, "settings": settings},
                timeout=600,  # 10 minutes for a headful session
            )
            console.print(f"  [green]sidecar response:[/] {json.dumps(resp, default=str)[:200]}")
    except SidecarError as e:
        console.print(f"[red]sidecar error: {e}[/]")
        raise typer.Exit(1) from e


@app.command()
def retro() -> None:
    """Weekly retro (P6): labels vs scores → a proposed profile tweak (read-only)."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT p.status, p.company_id, p.final_score, c.tier FROM postings p "
            "LEFT JOIN companies c ON p.company_id = c.id "
            "WHERE p.status IN ('interested', 'packet:drafting', 'packet:needs_input', "
            "'packet:ready', 'applied', 'dismissed')"
        ).fetchall()
    finally:
        conn.close()
    by_status: dict[str, int] = {}
    by_company: dict[str, list] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        key = r["company_id"] or "?"
        by_company.setdefault(key, []).append(r)

    t = Table(title=f"outcomes ({len(rows)} labelled postings)")
    t.add_column("status")
    t.add_column("count", justify="right")
    for k in sorted(by_status):
        t.add_row(k, str(by_status[k]))
    console.print(t)

    top = sorted(
        by_company.items(),
        key=lambda kv: sum(1 for r in kv[1] if r["status"] not in ("dismissed",)),
        reverse=True,
    )[:8]
    console.print("most-engaged companies (interested+applied, not dismissed):")
    for company, rs in top:
        tiers = {r["tier"] for r in rs if r["tier"]}
        avg = None
        scores = [r["final_score"] for r in rs if r["final_score"] is not None]
        if scores:
            avg = sum(scores) / len(scores)
        console.print(
            f"  {company}: {len(rs)} postings"
            + (f" · tier {', '.join(sorted(str(x) for x in tiers))}" if tiers else "")
            + (f" · avg score {avg:.0f}" if avg else "")
        )
    console.print(
        "[dim]proposal: promote companies with sustained engagement and check "
        "whether their tier in profile.yaml matches reality. No file is changed "
        "by retro — edit profile.yaml yourself.[/]"
    )


# ── db ───────────────────────────────────────────────────────────────────────


@db_app.command("init")
def db_init() -> None:
    """Create the SQLite database + schema (idempotent)."""
    path = db.init_db()
    console.print(f"[green]initialised[/] {path}")


@db_app.command("status")
def db_status() -> None:
    """Show DB path, schema version, row counts."""
    status = db.db_status()
    if not status["exists"]:
        console.print(f"[yellow]not initialised[/] ({status['path']}) — run `jobscout db init`")
        raise typer.Exit(1)
    table = Table(title="database")
    table.add_column("key")
    table.add_column("value", overflow="fold")
    table.add_row("path", status["path"])
    table.add_row("schema", f"v{status['schema_version']} (expected v{status['expected_version']})")
    table.add_row("size", f"{status['size_bytes']:,} bytes")
    for k, v in status["counts"].items():
        table.add_row(k, str(v))
    console.print(table)


# ── config ───────────────────────────────────────────────────────────────────


@config_app.command("check")
def config_check() -> None:
    """Validate all config files."""
    ok = True
    for label, loader in (
        ("settings.yaml", load_settings),
        ("models.yaml", load_models_cfg),
        ("profile.yaml", load_profile),
        ("watchlist.yaml", load_watchlist),
    ):
        try:
            loader()
            console.print(f"[green]✓[/] {label}")
        except ConfigError as e:
            ok = False
            console.print(f"[red]✗[/] {label}: {e}")
    if ok:
        console.print("[green]all configs valid[/]")
    else:
        raise typer.Exit(1)


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration (with env summary)."""
    try:
        settings = load_settings()
        models = load_models_cfg()
        profile = load_profile()
        watchlist = load_watchlist()
    except ConfigError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    console.print("[bold]settings[/]")
    console.print(settings.model_dump_json(indent=2))
    console.print("[bold]models[/]")
    console.print(models.model_dump_json(indent=2))
    console.print("[bold]profile[/]")
    console.print(profile.model_dump_json(indent=2))
    console.print("[bold]watchlist[/]")
    counts = {k: len(v) for k, v in watchlist.model_dump().items()}
    console.print(counts)
    env = load_env()
    env_summary = {k: ("***set***" if v else "empty") for k, v in env.items()}
    console.print("[bold]env[/]", env_summary)


# ── resume ───────────────────────────────────────────────────────────────────


@resume_app.command("validate")
def resume_validate() -> None:
    """Validate master_resume schema + referential integrity."""
    try:
        issues = validate_resume()
    except ResumeError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(1) from e
    if not issues:
        console.print("[green]✓ master resume valid[/]")
        return
    has_error = False
    for i in issues:
        color = "red" if i.level == "error" else "yellow"
        console.print(f"[{color}]{i.level.upper()}[/] {i.msg}")
        has_error |= i.level == "error"
    if has_error:
        raise typer.Exit(1)


@resume_app.command("fields")
def resume_fields() -> None:
    """Dump the canonical application field map (PLAN §5.3)."""
    try:
        entries = field_map()
    except ResumeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    table = Table(title="canonical application field map")
    table.add_column("canonical key", style="bold")
    table.add_column("value", overflow="fold")
    table.add_column("source", style="dim", overflow="fold")
    table.add_column("note", style="dim", overflow="fold")
    for e in entries:
        value = "" if e.value is None else (str(e.value) if e.value is not False else "false")
        table.add_row(e.canonical_key, value, e.source, e.note)
    console.print(table)


if __name__ == "__main__":
    app()
