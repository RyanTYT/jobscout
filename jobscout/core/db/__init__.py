"""core/db — the database layer.

schema.py: DDL, migrations, connect/init/status.
queries.py: the CRUD surface.
Both re-exported here: `from jobscout.core import db` works exactly as
it did when this was a single module.
"""

from jobscout.core.db import queries as _q
from jobscout.core.db.queries import *  # noqa: F401,F403

# private names tests/fixtures poke at directly
_POSTING_SELECT = _q._POSTING_SELECT
from jobscout.core.db.schema import *  # noqa: F401,F403
from jobscout.core.db.schema import (  # noqa: F401
    SCHEMA_VERSION,
    _migrate,
    _utcnow,
    db_path,
    sha256,
    slugify,
)
