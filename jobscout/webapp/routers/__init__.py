"""webapp.routers — one module per page area; register_all wires them."""

from jobscout.webapp.routers import (  # noqa: F401
    applications,
    companies,
    discovery,
    inbox,
    ops,
    profile,
)


def register_all(app) -> None:
    for mod in (inbox, companies, discovery, applications, ops, profile):
        mod.register(app)
