"""scoring/score_run.py — the scoring worklist orchestrator.

One entry point, `score_unscored()`, shared by the daily pipeline, the
`jobscout score` verb, and the dashboard's "Rescore postings" button. Decides
*which* postings need scoring (never-scored, or scored against a different
profile), then hands the worklist to the batch scorer.

Scope selection (the profile-hash gate):
  * auto        — full pass iff the profile hash has moved since the last pass
                  that completed, else only never-scored postings
  * incremental — never-scored postings only (what the daily sweep wants: the
                  sweep's own rule-pass rows, nothing else)
  * full        — every eligible rule-pass posting, cache permitting

Resume: postings.llm_cache_key is written as scoring proceeds and keys into
llm_cache, which encodes the profile hash. So a pass cut short by the row
limit or the spend cap re-runs only the rows it did not reach — no progress is
lost and nothing is paid for twice.
"""

from __future__ import annotations

from jobscout.clients.llm import LlmClient
from jobscout.core import db
from jobscout.core.config import load_profile, load_settings, profile_hash
from jobscout.core.schema import ProfileCfg
from jobscout.scoring import llm_bulk

SCOPES = ("auto", "incremental", "full")


def score_unscored(
    conn,
    *,
    limit: int = 10_000,
    scope: str = "auto",
    ids: set[str] | None = None,
    profile: ProfileCfg | None = None,
    llm: LlmClient | None = None,
    record_run: bool = True,
    verbose: bool = True,
) -> dict:
    """Score postings that need it. Returns a stats dict; never raises for
    LLM problems — scoring degrades to rule-only rather than breaking a run.

    `ids` restricts the worklist to a subset (the daily sweep passes the rows
    its own rule filter just accepted). `record_run=False` skips the runs-table
    bookkeeping for callers that already own a run row.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")

    profile = profile if profile is not None else load_profile()
    settings = load_settings()
    batch_size = settings.discovery.pipeline.llm_batch_size
    phash = profile_hash(profile)

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    if llm is None:
        llm = LlmClient(conn=conn)
    if not llm.available:
        say("LLM scoring skipped (no API key) — rule-only "
            "(set JOBSCOUT_LLM_API_KEY in .env)")
        return _result(scope, phash, skipped="no api key")

    full = _is_full_pass(conn, scope, phash)
    say(f"scoring: scope={scope} ({'full pass' if full else 'never-scored only'})"
        f" · profile {phash[:12]} · batch {batch_size}")

    if scope == "incremental" or ids is not None:
        rows = db.unscored_rule_pass(conn, limit=10_000)
        if ids is not None:
            rows = [r for r in rows if r["id"] in ids]
        rows = rows[:limit]
    elif full:
        rows = db.rule_pass_for_rescore(conn, limit=limit)
    else:
        rows = db.unscored_rule_pass(conn, limit=limit)

    if not rows:
        say("nothing to score — every eligible posting has a score for this profile")
        _mark_done(conn, phash, full, record_run, None)
        return _result(scope, phash, skipped="nothing to score")

    run_id = db.record_run(conn, "score") if record_run else None
    if full:
        # record the in-flight pass so an interrupted run leaves a trace and
        # the next run knows this profile is only partly done
        db.set_scoring_state(
            conn, pending_profile_hash=phash, pending_limit=limit,
            pending_remaining=len(rows),
        )

    say(f"scoring {len(rows)} postings in batches of {batch_size} …")
    stats = llm_bulk.score_postings_batch(
        conn, rows, profile, llm, batch_size=batch_size)

    say(
        f"scored {stats['scored']} · cached {stats['cached']} · "
        f"batches {stats['batches']} · requests {stats['requests']} · "
        f"fallback {stats['fallback']} · errors {stats['errors']} · "
        f"${stats['cost']:.4f}"
        + ("  [CAP HIT]" if stats["capped"] else "")
    )

    _mark_done(conn, phash, full, record_run, run_id, stats=stats,
               attempted=len(rows))
    out = _result(scope, phash)
    out.update(stats)
    return out


# ── the profile-hash gate ─────────────────────────────────────────────────────


def _is_full_pass(conn, scope: str, phash: str) -> bool:
    if scope == "full":
        return True
    if scope == "incremental":
        return False
    st = db.get_scoring_state(conn)
    # full iff the profile moved on since the last pass that completed, or a
    # previous full pass at this hash never finished
    return st["scored_profile_hash"] != phash


def _mark_done(conn, phash: str, full: bool, record_run: bool,
               run_id: int | None, stats: dict | None = None,
               attempted: int = 0) -> None:
    """Record how far scoring got. A pass only counts as *done* when it was
    not cut short — otherwise the marker would claim a profile is fully scored
    while rows remain, and the next run would skip them."""
    remaining = db.count_unscored_at_hash(conn)

    if full:
        clean = bool(stats) and not stats.get("capped") and remaining == 0
        fields: dict = {"pending_remaining": remaining}
        if clean:
            st = db.get_scoring_state(conn)
            fields["scored_profile_hash"] = phash
            fields["pending_profile_hash"] = None
            fields["full_passes"] = int(st["full_passes"] or 0) + 1
        db.set_scoring_state(conn, **fields)
    elif stats:
        db.set_scoring_state(conn, pending_remaining=remaining)

    if record_run and run_id is not None:
        db.finish_run(
            conn, run_id,
            stats={**(stats or {}), "scope_profile_hash": phash,
                   "attempted": attempted, "remaining": remaining},
            cost_usd=(stats or {}).get("cost", 0.0),
            ok=not (stats or {}).get("capped", False),
        )
        db.set_scoring_state(conn, last_run_id=run_id)


def _result(scope: str, phash: str, skipped: str | None = None) -> dict:
    out = {"scored": 0, "cached": 0, "batches": 0, "requests": 0,
           "fallback": 0, "cost": 0.0, "capped": False, "errors": 0,
           "scope": scope, "profile_hash": phash}
    if skipped:
        out["skipped"] = skipped
    return out
