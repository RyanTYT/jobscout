# webapp/stores/ — the config editors

Every user-editable file (yaml + .env) gets ONE store. All stores sit on
one engine — the patch/rollback machinery is written once.

```
stores/
  config_store.py     THE engine: comment-preserving line patches
                      (set_key / set_list_block / find_key), env upsert/drop,
                      commit() = mutate → write → validate → rollback
  profile_store.py    resume.yaml: 21 editable fields, block rewrites
  targeting_store.py  profile.yaml target block (hunting profile editor)
  models_store.py     models.yaml (tiers + caps) + provider price refresh
  settings_store.py   settings.yaml (agent caps, search engine block)
  key_store.py        .env: LLM key, CSE keys, Brave key (masked tails)
  watchlist_store.py  thin form adapter → core/watchlist.update_entry
```

**The contract** (from the engine): untouched keys, inline comments, and
file headers survive every save; a save that fails pydantic validation
rolls the file back and the error flashes on the page; structure
mismatches are loud. Each store keeps only its file-specific recipe +
its own exception type.

**Testing landmine:** stores resolve paths through
`jobscout.core.paths` at CALL time — tests patch
`paths.config_dir`/`paths.env_path` (ONE attribute) and nothing leaks.
**Tests:** `tests/test_targeting.py`, `tests/test_key_store.py`,
`tests/test_config_exposure.py`.
