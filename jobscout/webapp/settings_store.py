"""webapp/settings_store.py — agent caps editor (config/settings.yaml).

Only the discovery.agent block is editable from the frontend (schedule,
step cap, cost cap, run-on-signal); everything else in settings.yaml is
operational plumbing with sane defaults. Line patches preserve inline
comments; pydantic validation rolls back on failure. The mode switch has
its own existing writer (set_discovery_mode).
"""

from __future__ import annotations

import re
from pathlib import Path

from jobscout.core import config as core_config

SCHEDULES = ("daily", "weekdays", "mon-wed-fri", "manual")


class SettingsStoreError(Exception):
    pass


def _path() -> Path:
    p = Path(core_config.config_dir()) / "settings.yaml"
    if not p.is_file():
        raise SettingsStoreError(f"missing config file: {p}")
    return p


def current():
    return core_config.load_settings().discovery.agent


def _set(lines: list[str], key: str, value: str) -> bool:
    """Patch `  agent:`'s 4-space `key:` line, preserving its comment."""
    # locate the agent block
    agent_i = None
    for i, line in enumerate(lines):
        if re.match(r"^  agent:\s*$", line):
            agent_i = i
            break
    if agent_i is None:
        return False
    pat = re.compile(
        rf"^(    ){re.escape(key)}:(\s*[^\n#]*)(\s+#.*)?$")
    for j in range(agent_i + 1, len(lines)):
        s = lines[j]
        if re.match(r"^  \w[\w-]*:\s*$", s) or re.match(r"^\w", s):
            return False                    # left the agent block
        m = pat.match(s)
        if m:
            lines[j] = f"    {key}: {value}{m.group(3) or ''}"
            return True
    return False


def save(*, schedule: str, max_steps: str, max_cost_usd: str,
          run_on_signal: bool) -> dict:
    if schedule not in SCHEDULES:
        raise SettingsStoreError(f"invalid schedule: {schedule!r}")
    try:
        steps = int(max_steps)
    except (TypeError, ValueError):
        raise SettingsStoreError("step cap must be a whole number") from None
    if steps < 1 or steps > 1000:
        raise SettingsStoreError("step cap must be between 1 and 1000")
    try:
        cost = float(max_cost_usd)
    except (TypeError, ValueError):
        raise SettingsStoreError("cost cap must be a number") from None
    if cost < 0 or cost > 100:
        raise SettingsStoreError("cost cap must be between 0 and 100")

    path = _path()
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    if not all([
        _set(lines, "schedule", schedule),
        _set(lines, "max_steps", str(steps)),
        _set(lines, "max_cost_usd", str(cost)),
        _set(lines, "run_on_signal", str(run_on_signal).lower()),
    ]):
        raise SettingsStoreError(
            "settings.yaml structure not recognised — the discovery.agent "
            "block should hold plain four-space keys")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        agent = core_config.load_settings().discovery.agent
        if (agent.max_steps != steps
                or abs(agent.max_cost_usd - cost) > 1e-9
                or agent.schedule != schedule
                or agent.run_on_signal != run_on_signal):
            raise core_config.ConfigError("round-trip mismatch")
        return {"agent": agent}
    except Exception as e:                       # rollback
        path.write_text(original, encoding="utf-8")
        raise SettingsStoreError(
            f"rejected by validation — file restored: {e}") from e
