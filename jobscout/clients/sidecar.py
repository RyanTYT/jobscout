"""JobPilot sidecar client (P7) — the Python-side subprocess wrapper.

Spawns the Node/Playwright sidecar (JobPilot/scraper/dist/index.js), speaks
the line protocol, and demuxes:
  - stdout: JSON lines with {id, ...} = responses (keyed by request id)
  - stderr: JSON lines with {type: "log", ...} = logs (captured for debugging)

Protocol contract (the sidecar's index.ts):
  request:  {"id": "<uuid>", "action": "<action>", "payload": {...}}\n
  response: {"id": "<uuid>", "result"?: ..., "error"?: "...", "event_type"?: ...}\n

Actions: ping, getAllScrapers, getAllFillers, scanForm, scrapeJobs,
         applyJobs, applyJobsByPayload, cancelScrapeJobs, cancelApplyJobs,
         killServer
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from jobscout.core import paths as core_paths


class SidecarError(Exception):
    pass


class SidecarTimeout(SidecarError):
    pass


class SidecarClient:
    """One sidecar process per client instance. Context manager supported."""

    def __init__(self, *, sidecar_path: str | None = None, headless: bool = True,
                 max_workers: int = 3):
        self._path = Path(sidecar_path) if sidecar_path else self._default_path()
        self._headless = headless
        self._max_workers = max_workers
        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._responses: dict[str, dict] = {}
        self._events: dict[str, list] = {}
        self._lock = threading.Lock()
        self._log_tail: list[str] = []

    @staticmethod
    def _env() -> dict:
        """os.environ overlaid with the app's .env — the convention every other
        env reader in this codebase follows (llm.py, key_store, email_tracker,
        ops/run.py). Reading os.environ alone meant a JOBSCOUT_SIDECAR_BIN set
        in .env was silently ignored, which is how the packaged app ended up
        looking for the sidecar in its own Application Support dir."""
        import os

        from jobscout.core.config import load_env

        return {**os.environ, **load_env()}

    @staticmethod
    def _default_path() -> Path:
        env_bin = SidecarClient._env().get("JOBSCOUT_SIDECAR_BIN")
        if env_bin:
            return Path(env_bin).expanduser()
        # settings.sidecar.path — bundled and seeded by init_home(), so unlike a
        # .env entry it survives a reinstall or a wiped runtime home. Resolved
        # against the config dir when relative, which is what makes a bare
        # "../JobPilot" work from a checkout.
        cfg_path = SidecarClient._configured_path()
        if cfg_path:
            return cfg_path
        repo = core_paths.repo_root()
        return repo / ".." / "JobPilot" / "scraper" / "dist" / "index.js"

    @staticmethod
    def _configured_path() -> Path | None:
        """The scraper binary implied by settings.sidecar.path, or None."""
        try:
            from jobscout.core.config import load_settings

            raw = (load_settings().sidecar.path or "").strip()
        except Exception:                         # noqa: BLE001 — optional config
            return None
        if not raw:
            return None
        p = Path(raw).expanduser()
        # A path ending in .js is the binary itself. Anything else means "the
        # JobPilot checkout" — decided on the NAME, not on is_dir(), so a path
        # that does not exist yet (a fresh clone, a typo) still resolves to the
        # binary inside it instead of being passed through as-is.
        if p.suffix == ".js":
            return p
        return p / "scraper" / "dist" / "index.js"

    @staticmethod
    def available(sidecar_path: str | None = None) -> bool:
        """True when the sidecar binary is built and Node is present."""
        env = SidecarClient._env()
        path = Path(sidecar_path) if sidecar_path else SidecarClient._default_path()
        node = env.get("JOBSCOUT_NODE_BIN") or shutil.which("node")
        return path.is_file() and node is not None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the sidecar process. Raises SidecarError if not built."""
        if self._process is not None and self._process.poll() is None:
            return
        if not self._path.is_file():
            raise SidecarError(
                f"sidecar not built: {self._path} — run "
                f"'cd {self._path.parent.parent} && npm run build-internal'"
            )
        import os

        node = os.environ.get("JOBSCOUT_NODE_BIN") or shutil.which("node")
        if node is None:
            raise SidecarError(
                "node not found — install Node.js ≥ 18, or point "
                "JOBSCOUT_NODE_BIN at a bundled node runtime")

        args = ["node", str(self._path)]
        if self._headless:
            args.append("--headless=true")
        args.append(f"--maxWorkers={self._max_workers}")

        self._process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # wait for the ready signal ({"type":"ready"} on stdout)
        deadline = time.time() + 15
        while time.time() < deadline:
            with self._lock:
                if any(r.get("type") == "ready" for r in self._responses.values()):
                    return
            if self._process.poll() is not None:
                raise SidecarError(
                    f"sidecar exited during startup (code {self._process.returncode})"
                )
            time.sleep(0.2)
        # ready not seen — but the process is alive; continue anyway
        # (some deployments may not send it immediately)

    def stop(self) -> None:
        """Graceful shutdown: send killServer, then terminate."""
        if self._process is None:
            return
        if self._process.poll() is None:
            try:
                self.request("killServer", {}, timeout=5)
            except (SidecarTimeout, SidecarError):
                pass
            try:
                self._process.stdin.close()
            except OSError:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    # ── protocol ─────────────────────────────────────────────────────────────

    def request(self, action: str, payload: dict, *, timeout: float = 120.0) -> dict:
        """Send a request and block until the response arrives.

        Returns the full response object: {id, result?, error?, event_type?}.
        Raises SidecarTimeout on timeout, SidecarError on protocol errors.
        """
        if self._process is None or self._process.poll() is not None:
            raise SidecarError("sidecar is not running — call start() first")

        req_id = str(uuid.uuid4())
        message = json.dumps({"id": req_id, "action": action, "payload": payload})
        with self._lock:
            self._responses.pop(req_id, None)

        try:
            self._process.stdin.write(message + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError) as e:
            raise SidecarError(f"stdin write failed: {e}") from e

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                response = self._responses.pop(req_id, None)
            if response is not None:
                if "error" in response and response["error"]:
                    raise SidecarError(
                        f"sidecar error ({action}): {response['error']}"
                    )
                return response
            if self._process.poll() is not None:
                raise SidecarError(
                    f"sidecar died during {action} (exit code {self._process.returncode})"
                )
            time.sleep(0.1)

        raise SidecarTimeout(f"timeout after {timeout}s waiting for {action}")

    def ping(self) -> bool:
        """Quick health check."""
        try:
            resp = self.request("ping", {"probe": 1}, timeout=10)
            return "result" in resp or resp.get("type") == "pong"
        except (SidecarError, SidecarTimeout):
            return False

    def scan_form(self, url: str, *, timeout: float = 30.0) -> dict:
        """Read-only form scan. Returns {url, fields: FieldSchema[]}."""
        resp = self.request("scanForm", {"url": url}, timeout=timeout)
        return resp.get("result") or {}

    def get_fillers(self) -> list[dict]:
        """List registered filler manifests."""
        resp = self.request("getAllFillers", {}, timeout=10)
        return resp.get("fillers") or resp.get("result", {}).get("fillers") or []

    def apply_jobs_by_payload(self, jobs: list[dict], profile: dict,
                              settings: dict, *, timeout: float = 60.0) -> dict:
        """Enqueue a headed apply run. Returns the first response (its "id"
        keys all later events for this run — feed it to drain_events)."""
        return self.request("applyJobsByPayload",
                            {"jobs": jobs, "profile": profile,
                             "settings": settings}, timeout=timeout)

    def scrape_sites(self, scraper_ids: list[str], *, keywords: str,
                     location: str = "", top_n: int | None = None,
                     timeout: float = 300.0) -> list[dict]:
        """Scrape job boards via the Playwright sidecar (bot-walled sites:
        LinkedIn, Wellfound, YC — the boards plain HTTP cannot touch).

        Blocking: requests scrapeJobs, then drains events until the
        sidecar's scrape:all-done (EndMsg) or the timeout. Returns the
        merged jobs (JobDetails dicts) from all requested scrapers.
        """
        filters = {
            "mode": "jobs", "seniorityLevels": [], "jobTypes": [],
            "remoteOnly": False, "sectors": [], "companyIds": [],
            "keywords": [keywords] if keywords else [],
            "location": location or "",
        }
        if top_n:
            filters["topN"] = top_n
        resp = self.request("scrapeJobs",
                            {"scraper_ids": scraper_ids, "filters": filters},
                            timeout=60.0)
        req_id = resp.get("id")
        if not req_id:
            return []
        jobs: list[dict] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.drain_events(req_id)
            finished = False
            for ev in events:
                if ev.get("event_type") == "EndMsg":
                    finished = True
                # each scraper emits its OWN scrape:done result — MERGE
                # them (keeping only the last would drop every board but
                # the final one)
                result = ev.get("result") or {}
                if isinstance(result.get("jobs"), list):
                    jobs.extend(result["jobs"])
            if finished:
                return jobs
            time.sleep(2)
        return jobs

    def drain_events(self, req_id: str) -> list[dict]:
        """Pop all sidecar events accumulated for a request id so far.

        applyJobsByPayload streams progress: apply:update → {result:
        ApplicationRecord} while running, apply:all-done → event_type
        "EndMsg". The webapp's run tracker polls this to update statuses.
        """
        with self._lock:
            events = self._events.pop(req_id, [])
        return events

    def get_scrapers(self) -> list[dict]:
        """List registered scraper manifests."""
        resp = self.request("getAllScrapers", {}, timeout=10)
        return resp.get("scrapers") or resp.get("result", {}).get("scrapers") or []

    @property
    def log_tail(self, n: int = 20) -> list[str]:
        """Last n log lines from stderr (for debugging)."""
        with self._lock:
            return self._log_tail[-n:]

    # ── internals ────────────────────────────────────────────────────────────

    def _read_stdout(self) -> None:
        """Background thread: read stdout lines, file responses by id."""
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # non-JSON stdout (console.log debris) — skip
                continue
            msg_id = msg.get("id")
            if msg_id:
                with self._lock:
                    self._responses[msg_id] = msg
                    self._events.setdefault(msg_id, []).append(msg)
            elif msg.get("type") == "ready":
                with self._lock:
                    self._responses["__ready__"] = msg

    def _read_stderr(self) -> None:
        """Background thread: read stderr lines (logs)."""
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self._log_tail.append(line)
                if len(self._log_tail) > 100:
                    self._log_tail = self._log_tail[-100:]

    # ── context manager ─────────────────────────────────────────────────────

    def __enter__(self) -> SidecarClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
