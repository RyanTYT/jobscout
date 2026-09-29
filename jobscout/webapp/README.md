# webapp/ — the dashboard

A FastAPI app + Jinja templates + vendored Bootstrap 5.3 (no build step).
Zero-JS works (plain forms); app.js progressively enhances (HTMX row
swaps, dropdowns, toasts, sorting, dark mode).

```
webapp/
  routes.py   the assembler: app + static mount + db init + register_all
  common.py   shared plumbing: TEMPLATES + globals, nav ctx, row/toast helpers
  ui.py       view-models: InboxFilters, Pagination, nav sections, linkify
  routers/    one module per page area (see routers/README.md)
  stores/     the config editors (see stores/README.md)
  runners/    background orchestration (see runners/README.md)
  templates/  Jinja (base + macros component library + per-page + partials)
  static/     vendored bootstrap + tokens.css + app.css + app.js
```

**CSS architecture:** tokens.css (root: the palette + one dark-mode
flip) → vendored bootstrap → app.css (the theme bridge mapping tokens
onto `--bs-*` + custom components). No literal colors outside tokens.css.

**Buttons can't fail silently:** every form flashes its error on the page
(/discovery/mode, posting prepare, profile save/upload, targeting, apply,
keys, run panel); every HTMX request has a global responseError → danger
toast handler in app.js.

**External links** (company rows, careers buttons, employer forms) go
through POST /open-url on the backend — the OS `open` — so they land in
the DEFAULT browser, not the Tauri webview.

**Tests:** `tests/test_routes_*.py` (per router), `tests/test_webapp.py`
(ui view-models + db filtering), `tests/test_webapp_sweep.py` (every
referenced URL must resolve to a registered route), `tests/conftest.py`
(shared fixtures: seed DB + client factory).
