"""core/db — the database layer.

schema.py: DDL, migrations, connect/init/status.
queries.py: the CRUD surface.
Both re-exported here: `from jobscout.core import db` works exactly as
it did when this was a single module (private names tests poke at
directly are re-exported explicitly — star imports skip them).
"""

from jobscout.core.db.queries import *  # noqa: F401,F403
from jobscout.core.db.queries import _POSTING_SELECT  # noqa: F401
from jobscout.core.db.schema import *  # noqa: F401,F403
from jobscout.core.db.schema import (  # noqa: F401
    SCHEMA_VERSION,
    _migrate,
    _utcnow,
    db_path,
    sha256,
    slugify,
)
