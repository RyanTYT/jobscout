# agent/ — the judgment layer

An LLM tool-loop that runs on top of the deterministic sources: it reads a
brief rendered from live state, then calls tools until it hits the step
or cost cap. Modes: off | pipeline | agent | hybrid (settings.yaml).

## brief.py — `build_brief(conn, profile, settings)`

Renders the agent's instructions from LIVE state: date, the hunting
profile (roles/stack/domains/levels, primary vs acceptable locations),
today's signals, unresolved funding mentions, and the caps. Two focused
variants: `JOBSCOUT_AGENT_FOCUS=profile` injects a PROFILE-FOCUSED block
(spend the whole budget on employer discovery, skip signal resolution).

The brief mandates TWO search kinds: (a) PROFILE-DRIVEN — queries built
from your target, hunting employers that fit; (b) SIGNAL-DRIVEN —
funding news, hiring posts. Rules: every add needs a one-line rationale;
no invented domains; promotion to tiers A/B/C stays with the owner.

## harness.py — `run_morning(...)`

The tool loop: system prompt + brief → LLM (agent tier, tool-calling) →
execute tool calls → loop, until the model writes a final summary or the
caps hit (`max_steps` tool calls, `max_cost_usd` hard stop). Output: the
morning report (`var/morning_reports/YYYY-MM-DD.md`), the tool log, cost.
Without a key: the deterministic sweep still runs, the harness is skipped
(except `--dry-run`, which uses a scripted fake model).

## tools.py — the tool surface

`TOOLS_SPEC` + `execute(name, args, ctx)` — the only capabilities the
agent has:

| Tool | Job |
|---|---|
| `get_context` | reads the DB: signals, companies, unresolved funding — the scrape results |
| `web_search` | real web search, 4 engines: cse / brave / llm (search-grounded model via LlmClient.complete) / ddg (ddgs, free no key). Provider order: settings `search.provider` or auto (cse→brave→llm→ddg). Engines resolve at CALL time. |
| `fetch` | any URL → title + ~3KB text (read-only network) |
| `db_query` | read-only SQL against the DB (writes rejected, LIMIT forced) |
| `add_company` | writes a watchlist candidate + auto-probes ATS boards (never invents domains) |
| `add_signal` | records an observation on a known company (one-line, sourced) |
| `write_note` | research note into `var/research/` (path-sanitized) |

**Tests:** `tests/test_agent.py` (the full loop with a scripted model,
step caps, brief content, tool contracts).
