# core/ — the kernel

Everything the rest of the app stands on: path resolution, config loading,
the config schema, the database, resume loading, and the watchlist substrate.
Nothing in here knows about HTTP or the dashboard.

| Module | Job |
|---|---|
| `paths.py` | THE path authority. `repo_root()` walks up from the package or honors `JOBSCOUT_HOME` (packaged app). All runtime outputs live under `var/` — `var_root()` migrates pre-var layouts automatically (moves the legacy dirs + rewrites the packets table's absolute paths in one pass). |
| `config.py` | YAML → pydantic. Resolves `config_dir()`/`env_path()` through the **paths module at call time** (one patch point for tests), reads `.env` (`load_env()`, never raises), and `set_discovery_mode()` writes quoted values into settings.yaml. |
| `schema.py` | The config models: `Settings` (discovery mode, agent caps, search engine), `Watchlist`/`WatchlistEntry`, `ModelsCfg` (per-tier models + caps), `ProfileCfg`/`TargetCfg` (the hunting profile), `MasterResume` (the resume domain model). |
| `db/` | A package: `schema.py` (DDL, `_migrate`, `connect`/`init_db`/`db_status`) + `queries.py` (every CRUD function — companies, postings, signals, packets, runs, llm_calls, apply_runs). `core.db` re-exports both; private names tests poke at are re-exported explicitly. |
| `resume.py` | Loads + validates `master_resume/resume.yaml` → `MasterResume`; the field map (canonical keys → values with sources). |
| `watchlist.py` | The company substrate: `load`/`save`/`find`/`add`/`promote` + `update_entry` (the ONE mutation layer — entry blocks, tier moves, rollback; the UI delegates here). |

**Invariants:** no other module may hardcode a data path — everything goes
through `paths`. The watchlist FILE is mutated only through
`core/watchlist.py`. Config schema lives only in `schema.py`.

**Tests:** `tests/test_config_exposure.py` (fixture patches
`jobscout.core.paths.config_dir` — the single provider), `tests/test_apply.py`
(JOBSCOUT_HOME relocation), `tests/test_packets.py`.
