"""test_routes_discovery — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations


def test_discovery_page_shows_hunting_profile(client):
    r = client.get("/discovery")
    assert r.status_code == 200
    assert "Hunting profile" in r.text
    assert "levels" in r.text and "locations" in r.text




def test_run_agent_now_returns_immediately(client, monkeypatch):
    """The run-agent button: 303 instantly, run happens in background."""
    import subprocess
    import threading
    import time

    done = threading.Event()

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            self.args = args

        def communicate(self, timeout=None):
            done.set()
            time.sleep(0.2)
            return "agent done", ""

        def kill(self):
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    r = client.post("/discovery/run", follow_redirects=False)
    assert r.status_code == 303
    assert "started=1" in r.headers["location"]
    assert done.wait(5)                      # the background thread fired

    status = client.get("/discovery/run-status")
    assert status.status_code == 200
    assert "every 3s" in status.text




def test_run_panel_error_callout(client):
    from jobscout.webapp.runners import agent_runner as _ar

    _ar._state.update(running=False, out="some output",
                      error="ConfigError: missing config file: models.yaml")
    try:
        r = client.get("/discovery/run-status")
        assert r.status_code == 200
        assert "run failed" in r.text
        assert "ConfigError: missing config file" in r.text
    finally:
        _ar._state.update(running=False, out="", error="")



def test_run_panel_has_button_and_spend(client):

    r = client.get("/discovery/run-status")
    assert 'action="/discovery/run"' in r.text      # button lives in the panel
    assert "agent spend" in r.text                   # spend refreshes with it



def test_run_panel_running_state_polls(client):
    from jobscout.webapp.runners import agent_runner as _ar

    _ar._state.update(running=True, out="", error="")
    try:
        r = client.get("/discovery/run-status")
        assert "every 3s" in r.text
        assert "full hunt running" in r.text
        assert "Run agent now" not in r.text         # button swapped for state
    finally:
        _ar._state.update(running=False, out="", error="")



def test_run_panel_renders_both_hunt_buttons(client):
    r = client.get("/discovery/run-status")
    assert "full hunt (sweep + agent)" in r.text
    assert "profile hunt only" in r.text
    assert 'name="focus" value="profile"' in r.text
    assert "title=" in r.text                       # tooltips explain them


def test_run_panel_renders_rescore_button(client):
    r = client.get("/discovery/run-status")
    assert "rescore postings" in r.text
    assert 'action="/discovery/rescore"' in r.text
    # the batch size is surfaced from settings, not hard-coded in the template
    assert "20 postings per call" in r.text


def test_rescore_runs_scoring_only(client, monkeypatch):
    """The rescore button must not chain the sweep or the agent."""
    import subprocess as sp

    from jobscout.webapp.runners import agent_runner

    seen = []

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            seen.append(list(args))

        def communicate(self, timeout=None):
            return "scored", ""

        def kill(self):
            pass

    monkeypatch.setattr(sp, "Popen", FakePopen)
    r = client.post("/discovery/rescore", follow_redirects=False)
    assert r.status_code == 303

    # _run directly: the POST already spawned the background thread
    agent_runner._run("rescore")
    assert seen, "the rescore button must launch a subprocess"
    for args in seen:
        assert args[1] == "score", "no sweep, no agent"
        assert "--all" not in args, (
            "scope is decided by the profile-hash gate, not forced on the CLI")


def test_discovery_page_shows_profile_hash_and_scoring_state(client):
    r = client.get("/discovery")
    assert "content hash" in r.text
    assert "scoring state:" in r.text
    # never fully scored yet on a fresh DB
    assert "never fully scored" in r.text



def test_run_agent_focus_flag_reaches_subprocess(client, monkeypatch):
    import subprocess as sp

    seen = {}

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            seen["args"] = args
            seen["env"] = kw.get("env")

        def communicate(self, timeout=None):
            return "done", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(sp, "Popen", FakePopen)
    r = client.post("/discovery/run", data={"focus": "profile"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen["env"]["JOBSCOUT_AGENT_FOCUS"] == "profile"



def test_run_cancel_kills_and_labels(client, monkeypatch):
    """Cancel mid-run: the process is killed, output labelled cancelled."""
    import subprocess
    import threading
    import time

    from jobscout.webapp.runners import agent_runner as _ar

    started = threading.Event()

    class FakePopen:
        returncode = None

        def __init__(self, args, **kw):
            self.killed = threading.Event()

        def communicate(self, timeout=None):
            started.set()
            self.killed.wait(5)                 # blocks until kill() interrupts
            return "partial output", ""

        def kill(self):
            self.returncode = -9
            self.killed.set()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = client.post("/discovery/run", data={"focus": "profile"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert started.wait(5)

    c = client.post("/discovery/run/cancel", follow_redirects=False)
    assert c.status_code == 303
    for _ in range(30):                        # wait for the thread to land
        if not _ar._state["running"]:
            break
        time.sleep(0.1)
    assert _ar._state["error"] == "run cancelled by you"



def test_discovery_reports_read_from_runtime_root(client, monkeypatch, tmp_path):
    """Reports live under the runtime root (JOBSCOUT_HOME in the packaged
    app) — the reader must not look inside the frozen bundle's templates
    dir, which showed an empty table.

    Asserted against /discovery/reports: that partial (webapp/reports.py) is
    now the single report surface, since the duplicate read-only "Morning
    reports" card was removed from the Discovery page."""
    from jobscout.core import paths as core_paths

    (tmp_path / "morning_reports").mkdir()
    (tmp_path / "morning_reports" / "2026-09-29.md").write_text(
        "# report", encoding="utf-8")
    monkeypatch.setattr(core_paths, "morning_reports_dir", lambda: tmp_path / "morning_reports")
    r = client.get("/discovery/reports")
    assert "2026-09-29" in r.text



def test_mode_failure_flashes_on_page(client, monkeypatch):
    from jobscout.core import config as core_config

    def boom(mode):
        raise core_config.ConfigError("yaml exploded")

    monkeypatch.setattr(core_config, "set_discovery_mode", boom)
    # routes import set_discovery_mode inside the handler from core.config
    r = client.post("/discovery/mode", data={"mode": "agent"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "mode change failed" in page.text





# ── no duplicate morning-reports section ─────────────────────────────────────
# The split-panel browser (Daily + Morning tabs) is the report surface; the
# page used to also carry a second read-only "Morning reports" card.

def test_discovery_has_no_duplicate_morning_reports_card(client):
    r = client.get("/discovery")
    assert r.status_code == 200
    assert "Morning reports" not in r.text
    # the browser is still mounted, and recent runs are untouched
    assert 'id="report-tabs"' in r.text
    assert "Recent runs" in r.text


def test_report_browser_partial_carries_both_tabs(client):
    r = client.get("/discovery/reports")
    assert r.status_code == 200
    assert "Run Reports" in r.text
    assert "Daily" in r.text and "Morning" in r.text
