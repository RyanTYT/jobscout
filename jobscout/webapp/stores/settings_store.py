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
from jobscout.core import paths as core_paths
from jobscout.webapp.stores import config_store as cs

SCHEDULES = ("daily", "weekdays", "mon-wed-fri", "manual")
SEARCH_PROVIDERS = ("auto", "cse", "brave", "llm", "ddg")


class SettingsStoreError(Exception):
    pass


def _path() -> Path:
    p = Path(core_paths.config_dir()) / "settings.yaml"
    if not p.is_file():
        raise SettingsStoreError(f"missing config file: {p}")
    return p


def current():
    return core_config.load_settings().discovery.agent


def current_search():
    return core_config.load_settings().search


def save_search(*, provider: str, llm_model: str) -> dict:
    """Write the search: block (appended when the file predates it)."""
    if provider not in SEARCH_PROVIDERS:
        raise SettingsStoreError(f"invalid provider: {provider!r}")
    llm_model = (llm_model or "").strip()
    if provider == "llm" and not llm_model:
        raise SettingsStoreError(
            "the llm engine needs a search model slug (e.g. "
            "openai/gpt-4o-mini:online or a sonar model)")
    path = _path()
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    model_line = f"  llm_model: {llm_model}" if llm_model else '  llm_model: ""'
    block = ["search:",
             f"  provider: {provider}",
             model_line]
    # find an existing top-level search: block to replace
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^search:\s*$", line):
            start = i
            break
    if start is not None:
        end = start + 1
        while end < len(lines) and (not lines[end] or lines[end][0] in " \t"
                                    or lines[end].startswith("#")):
            end += 1
        lines[start:end] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += block
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        search = core_config.load_settings().search
        if search.provider != provider:
            raise core_config.ConfigError("round-trip mismatch")
        return {"search": search}
    except Exception as e:
        path.write_text(original, encoding="utf-8")
        raise SettingsStoreError(
            f"rejected by validation — file restored: {e}") from e


def _agent_block(lines: list[str]) -> tuple[int, int]:
    """(start, end) of the 4-space discovery.agent block."""
    agent_i = cs.find_key(lines, "agent", indent="  ")
    if agent_i is None:
        raise cs.StructureError("no discovery.agent block")
    end = len(lines)
    for j in range(agent_i + 1, len(lines)):
        s = lines[j]
        if re.match(r"^  \w[\w-]*:\s*$", s) or (s and s[0] not in " \t"):
            end = j
            break
    return agent_i + 1, end


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

    def _mutate(lines):
        start, end = _agent_block(lines)
        cs.require(cs.set_key(lines, "schedule", schedule, indent="    ",
                              start=start, end=end), "agent.schedule")
        cs.require(cs.set_key(lines, "max_steps", str(steps), indent="    ",
                              start=start, end=end), "agent.max_steps")
        cs.require(cs.set_key(lines, "max_cost_usd", str(cost),
                              indent="    ", start=start, end=end),
                   "agent.max_cost_usd")
        cs.require(cs.set_key(lines, "run_on_signal",
                              str(run_on_signal).lower(), indent="    ",
                              start=start, end=end), "agent.run_on_signal")

    def _validate():
        agent = core_config.load_settings().discovery.agent
        if (agent.max_steps != steps
                or abs(agent.max_cost_usd - cost) > 1e-9
                or agent.schedule != schedule
                or agent.run_on_signal != run_on_signal):
            raise core_config.ConfigError("round-trip mismatch")
        return {"agent": agent}

    return cs.commit(_path(), _mutate, _validate,
                     error_cls=SettingsStoreError,
                     structure_msg="settings.yaml structure not "
                                   "recognised — the discovery.agent block "
                                   "should hold plain four-space keys")
