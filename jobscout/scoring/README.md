# scoring/ — the gates

Two layers between raw postings and your inbox; both stamp postings in
the DB (`rule_pass`, LLM scores) so they run once per posting.

## rules.py — the deterministic gate (free)

`rule_filter(posting, profile) -> (passed, reasons)`:

1. **Dealbreakers** first (clearance_required, on_call_heavy — patterns
   from `profile.dealbreakers`) — instant fail.
2. **Role match**: tech-marker in title (engineer/developer/quant/...)
   AND a role keyword from `profile.target.roles` in title or description.
3. **Seniority**: an EXCLUSION list — titles that say
   senior/staff/lead/principal/manager fail unless that level is in
   `target.seniorities`; unlabeled titles (most "mid" postings) pass.
4. **Locations**: substring match against `target.locations` (union with
   `primary_locations`); unknown/remote pass (recall-first — dark-pool
   postings often lack clean location data).
5. **Remote**: rejected only when `target.remote.allowed` is false.

`guess_seniority(title)` derives the level token from the title when the
source didn't provide one.

## llm_bulk.py — the cheap tier (pennies, cached)

`score_postings(conn, ...)` scores rule-passed postings with the bulk
tier: fit/company-quality/opportunity rubric weights from
`config/profile.yaml`, JSON-mode output, **content-hash + profile_version
keyed cache** (`llm_cache`) — identical postings never re-score; bumping
`profile_version` re-scores. Records each call in `llm_calls` (spend
meter). Without a key: degrades to rule-only, noted in the digest.

**Tests:** `tests/test_llm_bulk.py`, `tests/test_webapp.py` (filter
layer), `tests/test_config_exposure.py` (rubric config).
