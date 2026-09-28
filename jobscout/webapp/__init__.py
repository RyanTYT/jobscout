"""jobscout webapp — FastAPI + Jinja2 + HTMX, loopback only, no build step."""

from jobscout.webapp.routes import create_app

__all__ = ["create_app"]
