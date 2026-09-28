"""webapp/apply.py — the apply launcher (PLAN §10 P9).

Turns selected packets into live application runs, two modes:

  automated — the posting's apply URL matches a JobPilot filler (greenhouse,
  lever, ashby, linkedin). One headed (visible) sidecar run is enqueued with
  pauseOnUncertainty=true: the filler completes the form, pausing — and
  leaving the browser window open for the human — wherever it would
  otherwise have to guess (missing field, cover letter, login, captcha).

  assisted — no filler knows this form. A browser window opens at the apply
  URL, tiled across the screen, with the packet's fill sheet one click away.

Both paths tile their windows across the screen: automated windows carry
position hints the sidecar passes to Playwright (--window-position /
--window-size); assisted windows use `open -na` Chrome args.

Nothing here fabricates data: the profile is the master resume's fill sheet,
and the claim-check gate has already vetted the packet content. The human
remains in the loop — automated fillers submit on their own, paused ones
wait for you in the open browser.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from jobscout.core import db
from jobscout.core import paths as core_paths
from jobscout.sidecar import SidecarClient

_SIDE_LOCK = threading.Lock()
_SIDE: SidecarClient | None = None
_SCREEN_CACHE: tuple[int, int] | None = None


class ApplyError(Exception):
    pass


# ── window tiling ────────────────────────────────────────────────────────────


def screen_size() -> tuple[int, int]:
    """Main display bounds (macOS Finder) — cached; falls back to 1440×900."""
    global _SCREEN_CACHE
    if _SCREEN_CACHE:
        return _SCREEN_CACHE
    w, h = 1440, 900
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        parts = [int(p) for p in out.split(",")]
        w, h = parts[2] - parts[0], parts[3] - parts[1]
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    _SCREEN_CACHE = (max(w, 800), max(h, 600))
    return _SCREEN_CACHE


def tile_grid(n: int) -> list[dict]:
    """Rects tiling the main screen for n windows (≤3 columns)."""
    n = max(1, n)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    w, h = screen_size()
    m, gap = 16, 8
    cw, ch = (w - 2 * m - (cols - 1) * gap) // cols, (h - 2 * m - (rows - 1) * gap) // rows
    return [
        {"x": m + (i % cols) * (cw + gap), "y": m + (i // cols) * (ch + gap),
         "width": cw, "height": ch}
        for i in range(n)
    ]


# ── sidecar singleton ────────────────────────────────────────────────────────


def get_sidecar() -> SidecarClient | None:
    """The shared headed sidecar process (one per webapp lifetime)."""
    global _SIDE
    if not SidecarClient.available():
        return None
    with _SIDE_LOCK:
        if _SIDE is None:
            client = SidecarClient(headless=False, max_workers=3)
            try:
                client.start()
            except Exception:
                return None
            _SIDE = client
        return _SIDE


def stop_sidecar() -> None:
    global _SIDE
    with _SIDE_LOCK:
        if _SIDE is not None:
            _SIDE.stop()
            _SIDE = None


# ── payload building ─────────────────────────────────────────────────────────


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _sheet_values(conn, packet: dict) -> dict[str, str]:
    """Flat canonical_key → value map from the packet's fill sheet."""
    import yaml

    fp = Path(packet["dir"] or "") / "fill_sheet.yaml"
    if not fp.is_file():
        return {}
    try:
        data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}
    return {f.get("canonical_key"): f.get("value") or ""
            for f in data.get("fields", [])
            if isinstance(f, dict)}


def sheet_missing_count(packet: dict) -> int | None:
    """Required-but-missing fields for a packet; None = no fill sheet."""
    import yaml

    fp = Path(packet["dir"] or "") / "fill_sheet.yaml"
    if not fp.is_file():
        return None
    try:
        data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return None
    return sum(1 for f in data.get("fields", [])
               if isinstance(f, dict) and f.get("required")
               and f.get("confidence") == "missing")


def _build_profile(conn, packets: list[dict]) -> dict:
    """JobPilot UserProfile from the (first) packet's fill sheet."""
    vals: dict[str, str] = {}
    for p in packets:
        vals = {k: v for k, v in _sheet_values(conn, p["row"]).items() if v}
        if vals:
            break
    full = (vals.get("full_name") or "").strip()
    parts = full.split(maxsplit=1) if full else []
    resume_path = ""
    for p in packets:                      # first packet's tailored resume
        rp = Path(p["row"]["dir"] or "") / "resume.md"
        if rp.is_file():
            resume_path = str(rp)
            break
    if not resume_path:
        rp = core_paths.master_resume_dir() / "resume.yaml"
        if rp.is_file():
            resume_path = str(rp)
    return {
        "firstName": parts[0] if parts else "",
        "lastName": parts[1] if len(parts) > 1 else "",
        "email": vals.get("email", ""),
        "phone": vals.get("phone", ""),
        "city": vals.get("city", ""),
        "state": vals.get("region", ""),
        "region": vals.get("region", ""),
        "country": vals.get("country", ""),
        "linkedinUrl": vals.get("linkedin", ""),
        "githubUrl": vals.get("github", ""),
        "portfolioUrl": vals.get("personal_website", ""),
        "resumePath": resume_path,
    }


def _filler_hostnames(client: SidecarClient) -> set[str]:
    hosts: set[str] = set()
    for manifest in client.get_fillers():
        for h in manifest.get("handles_hostnames") or []:
            if manifest.get("health") != "broken":
                hosts.add(str(h).lower())
    return hosts


def _url_matches(hostnames: set[str], url: str) -> bool:
    host = _hostname(url)
    return any(host == h or host.endswith("." + h) for h in hostnames)


# ── assisted windows ─────────────────────────────────────────────────────────


def _chrome_path() -> str | None:
    for p in ("/Applications/Google Chrome.app",):
        if Path(p).is_dir():
            return p
    return None


def open_assisted(url: str, rect: dict) -> bool:
    """Open a tiled browser window at the URL. Chrome when present (tiled),
    the default browser otherwise (untiled). True when tiled."""
    chrome = _chrome_path()
    try:
        if chrome:
            subprocess.Popen(
                ["open", "-na", chrome, "--args", "--new-window",
                 f"--window-position={rect['x']},{rect['y']}",
                 f"--window-size={rect['width']},{rect['height']}",
                 url],
                start_new_session=True,
            )
            return True
        subprocess.Popen(["open", url], start_new_session=True)
    except OSError:
        return False
    return False


# ── the launcher ─────────────────────────────────────────────────────────────


def refresh_runs(conn) -> None:
    """Drain sidecar events into apply_runs statuses (call on page views)."""
    global _SIDE
    with _SIDE_LOCK:
        client = _SIDE
    if client is None:
        return
    try:
        active = conn.execute(
            "SELECT DISTINCT packet_id FROM apply_runs"
            " WHERE status IN ('launched', 'running')"
        ).fetchall()
        if not active:
            return
        packets = {r[0] for r in active}
        for req_id in list(getattr(client, "_tracked_apply_ids", []) or []):
            for ev in client.drain_events(req_id):
                record = ev.get("result") or {}
                pid = (record.get("job") or {}).get("id") or record.get("id")
                if pid not in packets:
                    continue
                status = record.get("status")
                if status == "filling":
                    db.update_apply_run(conn, packet_id=pid, status="running")
                elif status == "submitted":
                    db.update_apply_run(conn, packet_id=pid, status="submitted",
                                        detail="submitted by filler")
                    db.set_posting_status(conn, _posting_id(conn, pid), "applied")
                elif status == "paused":
                    db.update_apply_run(
                        conn, packet_id=pid, status="paused",
                        detail=f"paused: {record.get('pauseReason') or '?'} — "
                               "browser window is open for you")
                elif status == "failed":
                    db.update_apply_run(
                        conn, packet_id=pid, status="failed",
                        detail=(record.get("error") or "failed")[:300])
    except Exception:
        pass  # status refresh is best-effort; never break a page render


def _posting_id(conn, packet_id: str) -> str | None:
    row = conn.execute("SELECT posting_id FROM packets WHERE id = ?",
                       (packet_id,)).fetchone()
    return row[0] if row else None


def launch_apply(conn, packet_ids: list[str]) -> dict:
    """Apply to the selected packets. Returns a summary for the flash UI."""
    packets = []
    for pid in packet_ids:
        pk = db.get_packet(conn, pid)
        if pk is None:
            continue
        posting = db.get_posting(conn, pk["posting_id"])
        if posting is None or not posting["url"]:
            db.record_apply_run(conn, packet_id=pid, mode="assisted",
                                status="failed", detail="no apply URL on posting")
            continue
        packets.append({"row": pk, "posting": posting,
                        "url": posting["url"], "host": _hostname(posting["url"])})

    if not packets:
        raise ApplyError("nothing applicable in the selection")

    client = get_sidecar()
    hostnames = _filler_hostnames(client) if client else set()
    automated = [p for p in packets if _url_matches(hostnames, p["url"])]
    assisted = [p for p in packets if p not in automated]

    results = {"automated": [], "assisted": [], "note": []}

    if client is None:
        results["note"].append(
            "JobPilot sidecar not built — every URL opened for manual fill "
            "(cd ../JobPilot/scraper && npm run build-internal)")

    if automated and client is not None:
            jobs = [{"id": p["row"]["id"], "title": p["posting"]["title"],
                     "company": p["posting"]["company_name"] or p["posting"]["company_id"],
                     "applyUrl": p["url"], "applyHostname": p["host"]}
                    for p in automated]
            profile = _build_profile(conn, automated)
            settings = {"pauseOnUncertainty": True,
                        "maxConcurrentApplications": 3,
                        "windows": tile_grid(len(jobs))}
            try:
                resp = client.apply_jobs_by_payload(jobs, profile, settings)
                req_id = resp.get("id")
                if req_id:
                    tracked = getattr(client, "_tracked_apply_ids", None)
                    if tracked is None:
                        tracked = []
                        client._tracked_apply_ids = tracked
                    tracked.append(req_id)
                for p in automated:
                    db.record_apply_run(
                        conn, packet_id=p["row"]["id"], mode="automated",
                        status="launched",
                        detail=f"headed filler run · {p['host']}")
                results["automated"] = [p["row"]["id"] for p in automated]
            except Exception as e:                # sidecar busy / died
                results["note"].append(f"sidecar apply failed: {e} — opened manually")
                assisted = packets
                results["automated"] = []

    if assisted:
        rects = tile_grid(len(assisted))
        for p, rect in zip(assisted, rects, strict=False):
            tiled = open_assisted(p["url"], rect)
            db.record_apply_run(
                conn, packet_id=p["row"]["id"], mode="assisted",
                status="opened",
                detail=(f"browser opened at {p['host']}"
                        + ("" if tiled else " (untiled — Chrome not found)")))
        results["assisted"] = [p["row"]["id"] for p in assisted]

    return results
