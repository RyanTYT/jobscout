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
    from jobscout.ats_probe import probe_company
    from jobscout.core.models import WatchlistEntry
    from jobscout.sources.ats.base import make_client

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
) -> None:
    """Probe ATS boards for a company without adding it to the watchlist."""
    from jobscout.ats_probe import probe_company
    from jobscout.sources.ats.base import make_client

    client = make_client()
    try:
        res = probe_company(name, domain, client=client)
    finally:
        client.close()
    if res["tokens"]:
        table = Table(title=f"ATS boards — {name}")
        table.add_column("provider")
        table.add_column("token")
        table.add_column("live jobs", justify="right")
        for provider, tok in res["tokens"].items():
            table.add_row(provider, tok, str(res["jobs"].get(provider, 0)))
        console.print(table)
    else:
        console.print(
            f"[yellow]no public ATS board found for {name}[/] — dark-pool candidate"
        )


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
def agent(morning: bool = typer.Option(False, "--morning")):
    """Execute the Morning Brief discovery agent (P5)."""
    _stub("agent", "P5", "harness + tools + caps + the discovery-mode switch")


@app.command()
def serve() -> None:
    """Serve the local dashboard at 127.0.0.1:8787 (P2)."""
    _stub("serve", "P2", "FastAPI + Jinja2 + HTMX inbox")


@app.command()
def prepare(posting: str = typer.Option(..., "--posting", help="posting id")):
    """Generate an application packet for a selected posting (P6)."""
    _stub(f"prepare --posting {posting}", "P6", "fill sheet + tailor + claim-check + typst render")


@app.command()
def mark(vid: str, status: str = typer.Argument(...)) -> None:
    """Record an outcome: applied | dismissed | withdrawn (P6)."""
    _stub(f"mark {vid} {status}", "P6", "status machine persistence")


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
