# webapp/routers/ — one module per page area

Each module exports `register(app)`; `routers/__init__.py`'s
`register_all` wires them in order. Route bodies live here; shared
plumbing comes from `webapp/common.py`; background machinery from
`webapp/runners/`.

| Module | Area | Routes |
|---|---|---|
| `inbox.py` | the triage surface | `/` (filters, pagination, HTMX row swaps + toasts), `/posting/{pid}` (+ status/prepare POSTs) |
| `companies.py` | the watchlist substrate | `/companies` (table + signal feed), add-by-URL (ATS board URLs auto-recognised), per-company editor + save, provenance badges, outreach generation (cold-email / linkedin reachout), company detail with **application history** (every packet incl. rejected/withdrawn — the re-apply record) + contact-email routes |
| `discovery.py` | agent control | `/discovery` (mode, hunting profile, agent caps, run panel), mode/targeting/agent-caps/pipeline saves, run launch/cancel, run-status partial. The **daily-sweep knob** (`POST /discovery/pipeline`) toggles each deterministic source (ATS boards, careers crawl, job sites, RSS) + the CSE queries/day cap |
| `applications.py` | packets + follow-through | `/applications` (stage groups, readiness, quiet-days badges, **OA/interview search** over all timeline entries, email activity panel, apply bar), packet detail (timeline: auto + manual OA/interview entries, AI follow-up nudge — draft-first), event POST/delete, follow-up POST + polled status, email-check, apply launcher POST, live run-log partial |
| `ops.py` | observability + config | `/ops` (spend, models & pricing, credentials: LLM/CSE/Brave/email keys, search engine selection, **email tracking card**), price refresh from provider, open-url (the external-link path that works in Tauri AND browsers) |
| `profile.py` | the master resume editor | `/profile` (21-field form), full YAML upload/download round-trip |
