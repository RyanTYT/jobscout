# webapp/routers/ — one module per page area

Each module exports `register(app)`; `routers/__init__.py`'s
`register_all` wires them in order. Route bodies live here; shared
plumbing comes from `webapp/common.py`; background machinery from
`webapp/runners/`.

| Module | Area | Routes |
|---|---|---|
| `inbox.py` | the triage surface | `/` (filters, pagination, HTMX row swaps + toasts), `/posting/{pid}` (+ status/prepare POSTs) |
| `companies.py` | the watchlist substrate | `/companies` (table + signal feed), add-by-URL (ATS board URLs auto-recognised), per-company editor + save, provenance badges |
| `discovery.py` | agent control | `/discovery` (mode, hunting profile, agent caps, run panel), mode/targeting/agent-caps saves, run launch/cancel, run-status partial |
| `applications.py` | packets + applying | `/applications` (readiness column, checkboxes, apply bar), packet detail, apply launcher POST, live run-log partial |
| `ops.py` | observability + config | `/ops` (spend, models & pricing, credentials: LLM/CSE/Brave keys, search engine selection), price refresh from provider, open-url |
| `profile.py` | the master resume editor | `/profile` (21-field form), full YAML upload/download round-trip |
