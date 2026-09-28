"""One-off P1 seeding: probe ATS boards for the researched candidate list,
write config/watchlist.yaml + the companies table. Idempotent — re-runnable.

Candidate list grounded in web research (2026-09-28): top prop/quant firms,
crypto-native market makers, and strong backend/infra targets (quant-weighted).
Every board token below is VERIFIED live by probing — no guesses survive.

Usage: .venv/bin/python scripts/seed_watchlist.py
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from jobscout import watchlist
from jobscout.ats_probe import probe_company
from jobscout.core import db
from jobscout.core.models import WatchlistEntry
from jobscout.sources.ats.base import make_client

# (tier, name, domain, hints, note)
# hints: extra token guesses per provider (tried before auto-guessed variants)
CANDIDATES = [
    # ── Tier A: quant / prop / market-making elite ──────────────────────────
    ("A", "Jane Street", "janestreet.com", {},
     "OCaml shop, flat culture; own careers page — dark-pool entry"),
    ("A", "Hudson River Trading", "hudsonrivertrading.com",
     {"greenhouse": ["hudsonrivertrading", "hrt"]}, ""),
    ("A", "Citadel Securities", "citadelsecurities.com",
     {"smartrecruiters": ["Citadel", "CitadelSecurities"]},
     "C++20/FPGA nanosecond shop; check Citadel too"),
    ("A", "Two Sigma", "twosigma.com", {"greenhouse": ["twosigma"]}, ""),
    ("A", "Jump Trading", "jumptrading.com",
     {"lever": ["jumptrading"], "greenhouse": ["jumptrading"]},
     "HFT/FPGA; Jump Crypto arm"),
    ("A", "DRW", "drw.com", {"greenhouse": ["drw"]}, "Cumberland = crypto arm"),
    ("A", "Optiver", "optiver.com",
     {"greenhouse": ["optiver"], "lever": ["optiver"]}, ""),
    ("A", "IMC Trading", "imc.com",
     {"greenhouse": ["imc", "imctrading"], "smartrecruiters": ["IMCTrading"]}, ""),
    ("A", "Susquehanna International Group", "sig.com",
     {"greenhouse": ["sig", "susquehanna"]}, ""),
    ("A", "Five Rings", "fiverings.com", {"greenhouse": ["fiverings"]},
     "small, secretive, high talent density"),
    ("A", "Tower Research Capital", "towerresearch.com",
     {"lever": ["towerresearchcapital", "towerresearch"], "greenhouse": ["towerresearch"]}, ""),
    ("A", "Squarepoint Capital", "squarepoint.com",
     {"greenhouse": ["squarepoint", "squarepointcapital"]}, ""),
    ("A", "XTX Markets", "xtx.tech", {"greenhouse": ["xtxmarkets", "xtx"]}, ""),
    ("A", "Virtu Financial", "virtu.com", {"greenhouse": ["virtu", "virtufinancial"]}, ""),
    ("A", "Flow Traders", "flowtraders.com", {"greenhouse": ["flowtraders"]},
     "ETP + crypto market maker"),
    ("A", "Headlands Technologies", "headlandstech.com", {"greenhouse": ["headlandstech"]}, ""),
    ("A", "Point72", "point72.com",
     {"greenhouse": ["point72"], "smartrecruiters": ["Point72"]}, ""),
    ("A", "Radix Trading", "radixtrading.com", {}, "own careers page — dark-pool entry"),

    # ── Tier B: crypto-native market makers / trading ───────────────────────
    ("B", "Wintermute", "wintermute.com",
     {"greenhouse": ["wintermute"], "lever": ["wintermute"]}, "crypto MM, Rust/C++"),
    ("B", "GSR", "gsr.io", {"greenhouse": ["gsr", "gsr-io"]}, "crypto MM"),
    ("B", "Keyrock", "keyrock.net", {"greenhouse": ["keyrock"]}, "crypto MM"),
    ("B", "B2C2", "b2c2.com", {}, "institutional crypto MM (SBI)"),
    ("B", "Presto Labs", "prestolabs.io", {"greenhouse": ["presto", "prestolabs"]},
     "Singapore HFT crypto"),
    ("B", "Caladan", "caladan.io", {}, "Singapore quant MM (ex-AlphaLab)"),
    ("B", "Selini Capital", "selinicapital.com", {}, "algo trading, CeFi+DeFi"),
    ("B", "FalconX", "falconx.io", {"greenhouse": ["falconx"]}, "crypto prime brokerage"),
    ("B", "Coinbase", "coinbase.com", {"greenhouse": ["coinbase"]}, ""),
    ("B", "Kraken", "kraken.com", {"greenhouse": ["krakenfx", "kraken"]}, ""),

    # ── Tier B: strong backend / infra (the 0.3) ────────────────────────────
    ("B", "Stripe", "stripe.com", {"lever": ["stripe"]}, ""),
    ("B", "Figma", "figma.com", {"greenhouse": ["figma"]}, ""),
    ("B", "Cloudflare", "cloudflare.com", {"greenhouse": ["cloudflare"]}, ""),
    ("B", "Datadog", "datadoghq.com", {"greenhouse": ["datadoghq", "datadog"]}, ""),
    ("B", "Vercel", "vercel.com", {"greenhouse": ["vercel"]}, ""),
    ("B", "Linear", "linear.app", {"greenhouse": ["linear"], "ashby": ["linear"]}, ""),
    ("B", "Ramp", "ramp.com", {"greenhouse": ["ramp"], "ashby": ["ramp"]}, ""),
    ("B", "Plaid", "plaid.com", {"greenhouse": ["plaid"]}, ""),
    ("B", "Anthropic", "anthropic.com", {"greenhouse": ["anthropic"]}, ""),
]


def main() -> int:
    console = Console()
    console.print(f"[bold]probing {len(CANDIDATES)} companies across 4 ATS providers …[/]")
    client = make_client()
    results: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(probe_company, name, domain, client=client, hints=hints): name
                for _tier, name, domain, hints, _note in CANDIDATES
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception as e:  # noqa: BLE001
                    results[name] = {"tokens": {}, "jobs": {}, "error": str(e)}
    finally:
        client.close()

    table = Table(title="probe results", show_lines=False)
    table.add_column("company", style="bold")
    table.add_column("tier")
    table.add_column("boards found", overflow="fold")
    for tier, name, _domain, _hints, _note in CANDIDATES:
        res = results.get(name, {"tokens": {}, "jobs": {}})
        if res.get("error"):
            table.add_row(name, tier, f"[red]error: {res['error']}[/]")
            continue
        found = ", ".join(
            f"{p}:{t} ({res['jobs'].get(p, 0)})" for p, t in res["tokens"].items()
        )
        if found:
            table.add_row(name, tier, f"[green]{found}[/]")
        else:
            table.add_row(name, tier, "[yellow]none — dark-pool entry[/]")
    console.print(table)

    w = watchlist.load()
    db.init_db()
    conn = db.connect()
    added = 0
    try:
        for tier, name, domain, _hints, note in CANDIDATES:
            res = results.get(name, {"tokens": {}, "jobs": {}})
            tokens = res["tokens"]
            entry = WatchlistEntry(
                name=name,
                domain=domain,
                ats=tokens,
                note=note or ("" if tokens else "no public ATS board — dark-pool entry"),
            )
            existing = watchlist.find(w, name)
            if existing is None:
                watchlist.add(w, entry, tier)
                added += 1
            else:
                # re-seed: merge newly probed tokens into the existing entry
                k, e = existing
                for provider, tok in tokens.items():
                    e.ats.setdefault(provider, tok)
                if not e.ats and not e.note:
                    e.note = "no public ATS board — dark-pool entry"
            db.upsert_company(
                conn, name=name, domain=domain, tier=tier,
                ats_tokens=tokens if tokens else None, notes=entry.note,
            )
        conn.commit()
    finally:
        conn.close()
    path = watchlist.save(w)
    console.print(f"[green]✓[/] watchlist saved: {path} ({added} new entries added)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
