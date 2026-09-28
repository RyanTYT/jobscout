"""Morning Brief harness (PLAN §1.2, §10 P5) — the headless agent loop.

Runs any model that speaks the LlmClient.chat interface (OpenAI-compatible
tool calling). Hard caps: step count + tier daily cost. All state changes go
through the agent tools, which go through core functions — the loop itself
cannot touch the DB directly. Every run ends with an auditable morning
report including a DB-change diff.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobscout.agent import tools as agent_tools
from jobscout.agent.brief import build_brief
from jobscout.llm import CapExceeded, LlmError, LlmResponse, ToolCall

MAX_TOOL_RESULT_CHARS = 4000


@dataclass
class FakeAgentModel:
    """Scripted model for --dry-run: exercises the full loop without a key."""

    script: list = field(default_factory=lambda: [
        ("tool", "get_context", {}),
        ("tool", "web_search", {"query": "quant trading firm hiring rust engineers"}),
        ("tool", "db_query", {"sql": "SELECT name, domain, tier FROM companies WHERE non_ats = 1"}),
        ("tool", "write_note", {
            "filename": "dry-run-note",
            "content": "Dry-run research note: harness loop exercised end-to-end.",
        }),
        ("final", None, "Dry run complete: get_context, web_search, db_query, and "
         "write_note executed. No LLM call was made."),
    ])
    _i: int = 0

    def chat(self, tier, messages, *, json_mode=True, cache_key=None, tools=None):
        kind, name, payload = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        if kind == "tool":
            return LlmResponse(
                text="", model="fake-agent", prompt_tokens=0, completion_tokens=0,
                cost_usd=0.0,
                tool_calls=[ToolCall(id=f"call-{self._i}", name=name, arguments=payload)],
            )
        return LlmResponse(
            text=payload, model="fake-agent", prompt_tokens=0, completion_tokens=0,
            cost_usd=0.0, tool_calls=None,
        )


def _snapshot(conn: sqlite3.Connection, wl) -> dict:
    tiers = {r["tier"]: r["n"] for r in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM companies GROUP BY tier")}
    return {
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "candidates": tiers.get("candidate", 0),
        "signals": conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
        "watchlist_candidates": len(wl.candidates),
    }


def _diff(before: dict, after: dict) -> list[str]:
    out = []
    for key in ("companies", "candidates", "signals", "watchlist_candidates"):
        delta = after.get(key, 0) - before.get(key, 0)
        if delta:
            out.append(f"{key}: {before.get(key, 0)} → {after.get(key, 0)} ({'+' if delta > 0 else ''}{delta})")
    return out


def run_morning(
    llm,
    conn: sqlite3.Connection,
    wl,
    profile,
    settings,
    env: dict,
    client,
    *,
    max_steps: int | None = None,
) -> dict:
    """Execute one Morning Brief. Returns stats incl. the report path.

    llm: LlmClient or FakeAgentModel (same chat interface).
    Caller saves the watchlist if watchlist_changed."""
    agent_cfg = settings.discovery.agent
    steps_cap = max_steps if max_steps is not None else agent_cfg.max_steps
    ctx = agent_tools.AgentCtx(
        conn=conn, wl=wl, profile=profile, settings=settings,
        env=env or {}, client=client,
    )
    before = _snapshot(conn, wl)
    watchlist_changed = False

    messages: list[dict] = [
        {"role": "system", "content": agent_tools.SYSTEM_PROMPT},
        {"role": "user", "content": build_brief(conn, profile, settings)},
    ]
    events: list[dict] = []
    steps = 0
    cost = 0.0
    final_text = ""
    cap_note = ""

    while True:
        if steps >= steps_cap:
            cap_note = f"STEP CAP REACHED ({steps_cap}) — truncated."
            break
        try:
            resp = llm.chat(
                "agent", messages, tools=agent_tools.TOOLS_SPEC, json_mode=False
            )
        except CapExceeded as e:
            cap_note = f"COST CAP: {e}"
            break
        except LlmError as e:
            cap_note = f"LLM ERROR: {e}"
            break
        cost += resp.cost_usd

        if not resp.tool_calls:
            final_text = resp.text or "(no final summary)"
            break

        messages.append({
            "role": "assistant",
            "content": resp.text or None,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in resp.tool_calls
            ],
        })
        for tc in resp.tool_calls:
            result = agent_tools.execute(tc.name, tc.arguments, ctx)
            if tc.name in ("add_company", "add_signal"):
                watchlist_changed = watchlist_changed or result.startswith("added") \
                    or result.startswith("signal recorded")
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": result[:MAX_TOOL_RESULT_CHARS],
            })
            events.append({
                "step": steps + 1, "tool": tc.name, "args": tc.arguments,
                "result": result[:400],
            })
            steps += 1

    after = _snapshot(conn, wl)
    diff = _diff(before, after)

    report_path = _write_report(
        events=events, steps=steps, cost=cost, final_text=final_text,
        cap_note=cap_note, diff=diff, before=before, after=after,
    )
    return {
        "steps": steps, "cost": cost, "events": events, "final": final_text,
        "cap_note": cap_note, "diff": diff, "watchlist_changed": watchlist_changed,
        "report": str(report_path),
    }


def _write_report(*, events, steps, cost, final_text, cap_note, diff, before, after) -> Path:
    from jobscout.core.paths import morning_reports_dir

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    out_dir = morning_reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today}.md"
    lines = [f"# jobscout morning report — {today}", ""]
    lines.append(f"**{steps} steps · ${cost:.4f} spent**"
                 + (f" · {cap_note}" if cap_note else ""))
    lines.append("")
    lines.append("## Final summary")
    lines.append("")
    lines.append(final_text)
    lines.append("")
    if diff:
        lines.append("## DB changes (auditable diff)")
        lines.append("")
        lines.extend(f"- {d}" for d in diff)
    else:
        lines.append("## DB changes")
        lines.append("")
        lines.append("- none")
    lines.append("")
    lines.append("## Tool log")
    lines.append("")
    for e in events:
        args = json.dumps(e["args"], ensure_ascii=False)
        lines.append(f"### step {e['step']} · `{e['tool']}`")
        lines.append(f"- args: `{args[:200]}`")
        lines.append(f"- result: {e['result']}")
        lines.append("")
    lines.append("---")
    lines.append(f"state: companies={after['companies']} (was {before['companies']}), "
                 f"signals={after['signals']} (was {before['signals']}), "
                 f"candidates={after['watchlist_candidates']} (was {before['watchlist_candidates']})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
