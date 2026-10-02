"""Tests for score_unscored: the profile-hash gate and resumability."""

from __future__ import annotations

import json
import sqlite3

import pytest

from jobscout.clients.llm import CapExceeded, LlmError, LlmResponse
from jobscout.core import db as core_db
from jobscout.core.config import profile_hash
from jobscout.core.schema import ProfileCfg
from jobscout.scoring import score_run


def _profile(stack=("rust",), locations=("Singapore",)) -> ProfileCfg:
    from jobscout.core.schema import RemoteCfg, TargetCfg

    return ProfileCfg(
        profile_version="2026-10-01.1",
        target=TargetCfg(
            roles=["quant developer"], seniorities=["junior"],
            locations=list(locations), primary_locations=list(locations),
            stack=list(stack), domains=["market-data"],
            remote=RemoteCfg(allowed=True, preference="hybrid"),
        ),
        dealbreakers=["on_call_heavy"],
    )


class FakeLlm:
    available = True

    def __init__(self, *, capped: bool = False, error: bool = False):
        self.calls = 0
        self.capped = capped
        self.error = error

    def chat(self, tier, messages, *, json_mode=True, cache_key=None):
        import re

        self.calls += 1
        if self.capped:
            raise CapExceeded("tier 'bulk' daily cap reached")
        if self.error:
            raise LlmError("HTTP 500")
        ids = re.findall(r"--- posting id: (\S+)", messages[1]["content"])
        return LlmResponse(
            text=json.dumps({"results": [
                {"id": pid, "fit": 70, "stack": [], "seniority": "junior",
                 "flags": [], "rationale": "ok", "red_flags": [],
                 "competition": "low"} for pid in ids]}),
            model="fake", prompt_tokens=100, completion_tokens=50,
            cost_usd=0.001,
        )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    c.execute(
        "INSERT INTO companies (id, name, domain, tier, ats_tokens, non_ats) "
        "VALUES ('co', 'Co', 'co.com', 'A', ?, 0)",
        (json.dumps({"greenhouse": "co"}),),
    )
    for i in range(6):
        c.execute(
            "INSERT INTO postings (id, source, company_id, url, url_hash, title, "
            "location, rule_pass, description, first_seen, last_seen, "
            "content_hash, status) VALUES (?, 'ats:greenhouse:co', 'co', ?, ?, "
            "?, 'Singapore', 1, 'rust quant', '2026-09-28T00:00:00Z', "
            "'2026-09-28T00:00:00Z', ?, 'new')",
            (f"p{i}", f"https://x/{i}", f"h{i}", f"Quant Dev {i}", f"c{i}"),
        )
    c.commit()
    return c


def _run(conn, profile, **kw):
    kw.setdefault("limit", 100)
    kw.setdefault("verbose", False)
    return score_run.score_unscored(conn, profile=profile, llm=FakeLlm(), **kw)


# ── the profile hash ──────────────────────────────────────────────────────────


def test_hash_is_order_stable():
    """Reordering a list must not invalidate the cache."""
    a = _profile(stack=("rust", "c++"), locations=("Singapore", "Tokyo"))
    b = _profile(stack=("c++", "rust"), locations=("Tokyo", "Singapore"))
    assert profile_hash(a) == profile_hash(b)


def test_hash_changes_with_content():
    assert profile_hash(_profile(stack=("rust",))) != profile_hash(
        _profile(stack=("rust", "python")))


def test_hash_ignores_the_version_counter():
    """The counter must not participate — that was the whole point."""
    a = _profile()
    b = _profile()
    b.profile_version = "2026-10-01.99"
    assert profile_hash(a) == profile_hash(b)


def test_cache_key_follows_the_profile_hash(conn):
    from jobscout.scoring import llm_bulk

    row = conn.execute(core_db._POSTING_SELECT + "WHERE p.id='p0'").fetchone()
    a = _profile(stack=("rust",))
    b = _profile(stack=("rust", "python"))
    assert llm_bulk.cache_key(a, row) != llm_bulk.cache_key(b, row)


# ── the gate ──────────────────────────────────────────────────────────────────


def test_first_run_is_a_full_pass(conn):
    p = _profile()
    st = score_run.score_unscored(conn, profile=p, llm=FakeLlm(), verbose=False)
    assert st["scored"] == 6
    assert core_db.get_scoring_state(conn)["scored_profile_hash"] == profile_hash(p)


def test_second_run_at_the_same_profile_scores_nothing(conn):
    """Unchanged profile → nothing to do, no LLM calls."""
    p = _profile()
    _run(conn, p)
    llm = FakeLlm()
    st = score_run.score_unscored(conn, profile=p, llm=llm, verbose=False)
    assert llm.calls == 0
    assert st.get("skipped") == "nothing to score"


def test_changed_profile_triggers_a_full_pass(conn):
    _run(conn, _profile(stack=("rust",)))
    st = _run(conn, _profile(stack=("rust", "python")))
    assert st["scored"] == 6, "every posting must be re-scored at the new profile"
    assert core_db.get_scoring_state(conn)["scored_profile_hash"] == profile_hash(
        _profile(stack=("rust", "python")))


def test_scope_incremental_never_full_rescores(conn):
    _run(conn, _profile(stack=("rust",)))
    # pretend a new posting arrived; incremental picks up only that one
    conn.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title, "
        "location, rule_pass, description, first_seen, last_seen, "
        "content_hash, status) VALUES ('p_new', 'ats:greenhouse:co', 'co', "
        "'https://x/new', 'hnew', 'Quant Dev', 'Singapore', 1, 'rust', "
        "'2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 'cnew', 'new')")
    conn.commit()
    st = _run(conn, _profile(stack=("rust",)), scope="incremental")
    assert st["scored"] == 1


def test_scope_full_widens_the_worklist_but_respects_the_cache(conn):
    """--all means 'consider every posting', not 'pay for every posting'.

    At an unchanged profile the whole worklist is considered but every row is
    a cache hit, so a forced full pass costs nothing — which is what makes
    re-running a rescore safe.
    """
    _run(conn, _profile())
    st = _run(conn, _profile(), scope="full")
    assert st["scored"] == 0
    assert st["cached"] == 6


# ── resume ────────────────────────────────────────────────────────────────────


def test_interrupted_pass_resumes_without_redoing_work(conn):
    """The row limit cuts a pass short; the next run finishes the remainder."""
    p = _profile()
    first = score_run.score_unscored(conn, profile=p, llm=FakeLlm(),
                                     limit=2, verbose=False)
    assert first["scored"] == 2

    # a cut-short pass must NOT be recorded as complete
    st = core_db.get_scoring_state(conn)
    assert st["scored_profile_hash"] != profile_hash(p)

    llm = FakeLlm()
    second = score_run.score_unscored(conn, profile=p, llm=llm,
                                      limit=100, verbose=False)
    assert llm.calls == 1, "4 remaining rows = one batch, not a re-run of 6"
    assert second["scored"] == 4
    assert core_db.get_scoring_state(conn)["scored_profile_hash"] == profile_hash(p)


def test_capped_pass_resumes(conn):
    p = _profile()
    score_run.score_unscored(conn, profile=p, llm=FakeLlm(capped=True),
                             verbose=False)
    assert core_db.get_scoring_state(conn)["scored_profile_hash"] != profile_hash(p)
    st = score_run.score_unscored(conn, profile=p, llm=FakeLlm(), verbose=False)
    assert st["scored"] == 6


def test_no_llm_is_a_clean_skip(conn):
    llm = FakeLlm()
    llm.available = False
    st = score_run.score_unscored(conn, profile=_profile(), llm=llm, verbose=False)
    assert st.get("skipped") == "no api key"


# ── a capped pass must be legible ─────────────────────────────────────────────


def test_capped_pass_records_why_it_stopped(conn):
    """`capped: True` alone is indistinguishable from a pass with nothing to
    do — which is how a cap-stopped pass read as 'the scorer skipped it'."""
    st = score_run.score_unscored(conn, profile=_profile(),
                                  llm=FakeLlm(capped=True), verbose=False)
    assert st["capped"] is True
    assert "daily cap" in st["cap_reason"]


def test_capped_reason_reaches_the_cli_summary(conn, capsys):
    score_run.score_unscored(conn, profile=_profile(), llm=FakeLlm(capped=True),
                             verbose=True)
    assert "stopped:" in capsys.readouterr().out


def test_uncapped_pass_has_no_reason(conn):
    st = score_run.score_unscored(conn, profile=_profile(), llm=FakeLlm(),
                                  verbose=False)
    assert st["capped"] is False
    assert st["cap_reason"] is None


# ── progress ──────────────────────────────────────────────────────────────────


def test_pending_remaining_tracks_progress_during_a_full_pass(conn, monkeypatch):
    """It used to be written once at pass start (as the full worklist size) and
    again at the end, so a panel polling mid-pass showed no movement."""
    from jobscout.scoring import llm_bulk

    seen: list[int] = []
    real = llm_bulk.score_postings_batch

    def wrapper(*a, on_progress=None, **kw):
        def spy(stats, done, total):
            if on_progress is not None:
                on_progress(stats, done, total)
            seen.append(core_db.get_scoring_state(conn)["pending_remaining"])
        return real(*a, on_progress=spy, **kw)

    monkeypatch.setattr(llm_bulk, "score_postings_batch", wrapper)
    score_run.score_unscored(conn, profile=_profile(), llm=FakeLlm(),
                             limit=100, verbose=False)

    assert seen, "the progress callback never fired"
    assert any(v < 6 for v in seen), (
        f"pending_remaining never dipped below the worklist size: {seen}")
    assert seen[-1] == 0


def test_progress_callback_reports_position(conn):
    """llm_bulk's on_progress fires before each batch and once at the end."""
    from jobscout.scoring import llm_bulk

    rows = [conn.execute(core_db._POSTING_SELECT +
                         "WHERE p.id=?", (f"p{i}",)).fetchone() for i in range(6)]
    snaps: list[tuple[int, int]] = []
    llm_bulk.score_postings_batch(
        conn, rows, _profile(), FakeLlm(), batch_size=2,
        on_progress=lambda s, done, total: snaps.append((done, total)))
    assert snaps[0] == (0, 6)
    assert snaps[-1] == (6, 6), "the final snapshot must report the pass complete"
    assert [d for d, _ in snaps] == [0, 2, 4, 6]


def test_progress_is_not_sent_when_no_callback(conn):
    from jobscout.scoring import llm_bulk

    rows = [conn.execute(core_db._POSTING_SELECT +
                         "WHERE p.id=?", (f"p{i}",)).fetchone() for i in range(2)]
    st = llm_bulk.score_postings_batch(conn, rows, _profile(), FakeLlm(),
                                       batch_size=2)
    assert st["scored"] == 2


def test_bad_scope_rejected(conn):
    with pytest.raises(ValueError):
        score_run.score_unscored(conn, profile=_profile(), llm=FakeLlm(),
                                 scope="nope")


def test_rescore_button_uses_an_effectively_unbounded_limit():
    """The button's --limit only bounds the worklist fetch; it must not be
    tuned to today's backlog."""
    from jobscout.webapp.runners import agent_runner

    cmd = agent_runner._commands("/bin/jobscout", "rescore")[0]
    limit = int(cmd[cmd.index("--limit") + 1])
    assert limit >= 100_000, f"rescore limit {limit} is a real ceiling"

