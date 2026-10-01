"""Run context — access the current run's logger from deep code.

Usage:
    from jobscout.core.run_context import get_run_logger
    get_run_logger()?.log("info", "scraper", "greenhouse: fetched 12 jobs")

If no run is active (CLI/cron), get_run_logger() returns None and callers no-op.
"""

from __future__ import annotations

from jobscout.core.run_logger import RunLogger

_current_logger: RunLogger | None = None


def set_run_logger(logger: RunLogger | None) -> None:
    global _current_logger
    _current_logger = logger


def get_run_logger() -> RunLogger | None:
    return _current_logger