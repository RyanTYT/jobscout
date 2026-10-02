"""webapp/agent_runner.py — the background agent-run machinery.

Owns the run state (focus, output, error, the killable process) and the
launch/cancel lifecycle, decoupled from HTTP: the discovery router
calls launch()/cancel(), the run-status partial reads state(). The
subprocess resolves its CLI both in dev (.venv/bin/jobscout) and in the
packaged app (sys.executable IS the frozen jobscout-server).

A full hunt chains `run --daily` (the deterministic sweep that fills
the inbox: ATS polling + careers crawl + scoring) THEN the morning
agent. A profile hunt runs the agent alone with a focused brief.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from jobscout.core import db

# the shared, module-level run state (one hunt at a time by design)
_state: dict = {"running": False, "out": "", "error": "", "focus": "",
                "proc": None, "cancel": False}

OUT_LIMIT = 8000


def state() -> dict:
    """A copy of the current run state for rendering."""
    return dict(_state)


def running() -> bool:
    return _state["running"]


def _strip_ansi(text: str) -> str:
    """Console colour codes have no business in a web page."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")


def _last_error(text: str) -> str:
    """The last `SomethingError: message` line from a CLI traceback."""
    hits = re.findall(r"[A-Za-z_]*(?:Error|Exception|ConfigError)\b[^\n]*",
                      text or "")
    return hits[-1].strip() if hits else ""


def _cli() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable                # the frozen jobscout-server
    return str(Path(sys.executable).parent / "jobscout")


FOCUSES = ("", "profile", "rescore")


def launch(focus: str = ""):
    """Start a run in a background thread; returns immediately."""
    if _state["running"]:
        return
    focus = focus if focus in FOCUSES else ""
    _state.update(running=True, out="", error="", focus=focus, cancel=False)
    threading.Thread(target=_run, args=(focus,), daemon=True).start()


def cancel():
    """Kill the in-flight hunt; partial output is kept and labelled."""
    if not _state["running"]:
        return
    _state["cancel"] = True
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()


def _commands(cli: str, focus: str) -> list[list[str]]:
    if focus == "rescore":
        # scoring only — no sweep, no agent. No --all: score_unscored's
        # profile-hash gate picks full-vs-incremental on its own.
        return [[cli, "score", "--limit", "10000"]]
    if focus == "profile":
        return [[cli, "agent"]]
    return [[cli, "run", "--daily"], [cli, "agent"]]


def _run(focus: str) -> None:
    try:
        env = {**os.environ}
        if focus:
            env["JOBSCOUT_AGENT_FOCUS"] = focus
        out, err, returncode = "", "", 0
        for cmd in _commands(_cli(), focus):
            # Popen (not subprocess.run) so a cancel can kill mid-run
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            _state["proc"] = proc
            o, e = proc.communicate()
            out += (o or "")
            err += (e or "")
            returncode = returncode or proc.returncode
        _state["out"] = _strip_ansi((out or "") + (err or ""))[-OUT_LIMIT:]
        if _state["cancel"]:
            _state["error"] = "run cancelled by you"
        elif returncode != 0:
            _state["error"] = _last_error(_state["out"]) or (
                f"the agent exited with code {returncode}")
        else:
            _state["error"] = ""
    except OSError as e:
        _state["out"] = f"failed to launch the jobscout agent: {e}"
        _state["error"] = _state["out"]
    finally:
        _state["proc"] = None
        _state["running"] = False
        _state["cancel"] = False


def agent_spend(conn) -> sqlite3.Row:
    """30-day agent-tier spend (the run panel's live stats)."""
    return conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
        "FROM llm_calls WHERE tier = 'agent' "
        "AND date(created_at) >= date('now', '-30 days')"
    ).fetchone()


def refresh_runs(conn) -> None:
    """Drain sidecar apply-run events into apply_runs statuses (best
    effort, never breaks a page render)."""
    try:
        _refresh(conn)
    except Exception:                          # noqa: BLE001
        pass


def _refresh(conn) -> None:
    active = conn.execute(
        "SELECT DISTINCT packet_id FROM apply_runs"
        " WHERE status IN ('launched', 'running')"
    ).fetchall()
    if not active:
        return
    packets = {r[0] for r in active}
    # the sidecar singleton is imported lazily: apply.py owns it
    from jobscout.webapp.runners.apply import get_sidecar

    client = get_sidecar()
    if client is None:
        return
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
                row = conn.execute(
                    "SELECT posting_id FROM packets WHERE id = ?",
                    (pid,)).fetchone()
                if row:
                    db.set_posting_status(conn, row[0], "applied")
            elif status == "paused":
                db.update_apply_run(
                    conn, packet_id=pid, status="paused",
                    detail=f"paused: {record.get('pauseReason') or '?'} — "
                           "browser window is open for you")
            elif status == "failed":
                db.update_apply_run(
                    conn, packet_id=pid, status="failed",
                    detail=(record.get("error") or "failed")[:300])
