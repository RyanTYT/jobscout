"""webapp/routes.py — the app assembler.

One job: build the FastAPI app, mount statics, init the DB, and wire the
page-area routers (webapp/routers/*). Route logic lives in the routers;
shared plumbing (templates, nav context, row/toast helpers) lives in
webapp/common.py; the background agent-run machinery in
webapp/agent_runner.py.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jobscout.core import db
from jobscout.webapp.common import BASE_DIR
from jobscout.webapp.routers import register_all


def create_app() -> FastAPI:
    app = FastAPI(title="jobscout", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.mount("/static",
              StaticFiles(directory=str(BASE_DIR / "static")),
              name="static")
    db.init_db()  # idempotent; also migrates schema
    register_all(app)
    return app
