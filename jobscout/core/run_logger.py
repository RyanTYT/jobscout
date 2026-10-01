"""RunLogger — pub/sub log collector for pipeline/agent runs.

Lives for the duration of one run. Deep code emits via run_context.get_run_logger();
SSE subscribers receive entries in real time. Also persists to a JSONL file
so logs survive restarts and can be reviewed later.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobscout.core import paths as core_paths


@dataclass
class LogEntry:
    timestamp: str
    level: str  # info | warn | error
    source: str  # pipeline | agent | scraper | llm | scoring | discovery
    message: str
    run_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "run_id": str(self.run_id),
        }


@dataclass
class RunLogger:
    run_id: str
    kind: str  # daily | agent | profile
    _entries: list[LogEntry] = field(default_factory=list)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _agent_calls: int = 0
    _total_spend: float = 0.0
    _file_handle: Any = None
    _file_path: Path | None = None

    def __post_init__(self) -> None:
        log_dir = core_paths.logs_dir()
        self._file_path = log_dir / "current-run.jsonl"
        # Truncate only when JOBSCOUT_LOG_TRUNCATE is set (first run in a
        # chain — agent_runner sets it before the first subprocess). The
        # second subprocess (agent after daily) appends to preserve the
        # daily run's output.
        import os as _os
        mode = "w" if _os.environ.get("JOBSCOUT_LOG_TRUNCATE") else "a"
        self._file_handle = open(self._file_path, mode, encoding="utf-8")

    def log(self, level: str, source: str, message: str) -> None:
        entry = LogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            level=level,
            source=source,
            message=message,
            run_id=self.run_id,
        )
        self._entries.append(entry)
        self._persist(entry)
        self._notify(entry)

    def _persist(self, entry: LogEntry) -> None:
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.write(json.dumps(entry.to_dict()) + "\n")
            self._file_handle.flush()

    def _notify(self, entry: LogEntry) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def snapshot(self, last_n: int = 200) -> list[LogEntry]:
        return self._entries[-last_n:]

    def record_agent_call(self, cost: float = 0.0) -> None:
        self._agent_calls += 1
        self._total_spend += cost

    @property
    def agent_calls(self) -> int:
        return self._agent_calls

    @property
    def total_spend(self) -> float:
        return self._total_spend

    def stats(self) -> dict[str, Any]:
        return {
            "agent_calls": self._agent_calls,
            "total_spend": self._total_spend,
            "log_count": len(self._entries),
        }

    def close(self) -> None:
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.close()