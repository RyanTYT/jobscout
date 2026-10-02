"""Tests for tier-1 bulk scoring: parse, cache, final-score math (P2)."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from jobscout.clients.llm import LlmResponse
from jobscout.core import db as core_db
from jobscout.core.schema import ProfileCfg
from jobscout.scoring import llm_bulk


class FakeLlm:
    """LlmClient stand-in returning a canned response; counts calls."""

    available = True

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def chat(self, tier, messages, *, json_mode=True, cache_key=None):
        self.calls += 1
        return LlmResponse(
            text=self.text, model="fake-model",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.001,
        )


PROFILE = ProfileCfg(profile_version="test-1")


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(core_db.SCHEMA)
    core_db._migrate(c)
    c.execute(
        "INSERT INTO companies (id, name, domain, tier, ats_tokens, non_ats) "
        "VALUES ('testco', 'TestCo', 'testco.com', 'A', ?, 0)",
        (json.dumps({"greenhouse": "testco"}),),
    )
    c.execute(
        "INSERT INTO postings (id, source, company_id, url, url_hash, title, location, "
        "rule_pass, description, first_seen, last_seen, content_hash, status) "
        "VALUES ('p1', 'ats:greenhouse:testco', 'testco', 'https://x/1', 'h1', "
        "'Senior Quant Developer', 'NYC', 1, 'rust and market data', '2026-09-28T00:00:00Z', "
        "'2026-09-28T00:00:00Z', 'ch1', 'new')",
    )
    c.commit()
    return c


CANNED = json.dumps({
    "fit": 82, "stack": ["rust", "market-data"], "seniority": "senior",
    "flags": ["quant"], "rationale": "Strong match.", "red_flags": [],
    "competition": "low",
})


class FakeBatchLlm:
    """Returns a canned reply per call. `reply` may be a str or a callable
    taking the batch size and returning a str."""

    available = True

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.sizes = []

    def chat(self, tier, messages, *, json_mode=True, cache_key=None):
        self.calls += 1
        ids = _ids_in(messages[1]["content"])
        self.sizes.append(len(ids))
        text = self.reply(ids) if callable(self.reply) else self.reply
        return LlmResponse(
            text=text, model="fake-model",
            prompt_tokens=100 * len(ids), completion_tokens=50 * len(ids),
            cost_usd=0.001,
        )


def _seed(conn, n: int, prefix: str = "b") -> list[sqlite3.Row]:
    """Add n unscored rule-pass postings; return their rows."""
    for i in range(n):
        conn.execute(
            "INSERT INTO postings (id, source, company_id, url, url_hash, title, "
            "location, rule_pass, description, first_seen, last_seen, "
            "content_hash, status) VALUES (?, 'ats:greenhouse:testco', "
            "'testco', ?, ?, ?, 'NYC', 1, 'rust and market data', "
            "'2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', ?, 'new')",
            (f"{prefix}{i}", f"https://x/{prefix}{i}", f"h{prefix}{i}",
             f"Senior Quant Developer {i}", f"ch{prefix}{i}"),
        )
    conn.commit()
    return conn.execute(
        core_db._POSTING_SELECT
        + f"WHERE p.id LIKE '{prefix}%' ORDER BY p.id"
    ).fetchall()


def _ids_in(prompt: str) -> list[str]:
    """The posting ids a batch prompt actually carries, in order."""
    return re.findall(r"--- posting id: (\S+)", prompt)


def _batch_reply(ids, fit: int = 70) -> str:
    return json.dumps({"results": [
        {"id": pid, "fit": fit, "stack": ["rust"], "seniority": "senior",
         "flags": [], "rationale": "ok", "red_flags": [],
         "competition": "low"}
        for pid in ids
    ]})


def test_score_posting_parses_and_caches(conn):
    llm = FakeLlm(CANNED)
    row = conn.execute(
        core_db._POSTING_SELECT + "WHERE p.id = 'p1'"
    ).fetchone()

    parsed = llm_bulk.score_posting(row, PROFILE, llm, conn)
    assert parsed is not None and parsed["fit"] == 82
    assert llm.calls == 1
    assert conn.execute("SELECT COUNT(*) c FROM llm_cache").fetchone()["c"] == 1

    # second call: cache hit, no LLM call
    parsed2 = llm_bulk.score_posting(row, PROFILE, llm, conn)
    assert parsed2 == parsed
    assert llm.calls == 1


def test_score_postings_updates_db(conn):
    llm = FakeLlm(CANNED)
    rows = conn.execute(core_db._POSTING_SELECT + "WHERE p.id = 'p1'").fetchall()
    stats = llm_bulk.score_postings(conn, rows, PROFILE, llm)
    assert stats["scored"] == 1 and stats["capped"] is False

    row = conn.execute("SELECT * FROM postings WHERE id = 'p1'").fetchone()
    assert row["fit_score"] == 82
    assert row["company_score"] == 90  # tier A
    assert row["final_score"] == pytest.approx(0.6 * 82 + 0.25 * 90 + 0.15 * 70, abs=0.2)
    saved = json.loads(row["llm_json"])
    assert saved["competition"] == "low"


def test_parse_rejects_garbage():
    assert llm_bulk._parse("not json at all") is None
    assert llm_bulk._parse('{"fit": "high"}') is None
    fenced = "```json\n" + CANNED + "\n```"
    assert llm_bulk._parse(fenced)["fit"] == 82


def test_compute_final_dark_pool_bonus():
    scores = llm_bulk.compute_final({"fit": 50, "competition": "high"}, "A", 1, 0)
    # dark-pool company (non_ats): opportunity 85 - 10 (high competition) = 75
    assert scores["opportunity"] == 75
    scores2 = llm_bulk.compute_final({"fit": 50, "competition": "low"}, "B", 0, 2)
    # many boards: 35 + 10 (low competition) = 45
    assert scores2["opportunity"] == 45


# ── batch scoring ─────────────────────────────────────────────────────────────


def test_batch_scores_everything_in_one_call(conn):
    rows = _seed(conn, 5)
    llm = FakeBatchLlm(_batch_reply)
    stats = llm_bulk.score_postings_batch(conn, rows, PROFILE, llm, batch_size=20)

    assert stats["scored"] == 5
    assert llm.calls == 1, "5 postings in one batch must be one LLM call"
    assert stats["batches"] == 1
    for r in rows:
        got = conn.execute(
            "SELECT final_score, llm_cache_key FROM postings WHERE id = ?",
            (r["id"],)).fetchone()
        assert got["final_score"] is not None
        # each posting carries its own cache key — never one key per batch
        assert got["llm_cache_key"] == llm_bulk.cache_key(PROFILE, r)


def test_batch_respects_batch_size(conn):
    rows = _seed(conn, 25)
    llm = FakeBatchLlm(_batch_reply)
    stats = llm_bulk.score_postings_batch(conn, rows, PROFILE, llm, batch_size=10)

    assert llm.calls == 3
    assert llm.sizes == [10, 10, 5]
    assert stats["scored"] == 25


def test_batch_skips_already_cached_rows(conn):
    rows = _seed(conn, 4)
    first = FakeBatchLlm(_batch_reply)
    llm_bulk.score_postings_batch(conn, rows, PROFILE, first, batch_size=4)
    assert first.calls == 1

    second = FakeBatchLlm(_batch_reply)
    stats = llm_bulk.score_postings_batch(conn, rows, PROFILE, second, batch_size=4)

    assert second.calls == 0, "a cached row must not cost an LLM call"
    assert stats["cached"] == 4
    assert stats["scored"] == 0


def test_batch_resumes_after_interruption(conn):
    """A pass cut short by the cap keeps what it scored; re-running picks up
    only the rows it never reached."""
    rows = _seed(conn, 4)
    llm = FakeBatchLlm(_batch_reply)
    llm_bulk.score_postings_batch(conn, rows[:2], PROFILE, llm, batch_size=2)

    done = {r["id"] for r in conn.execute(
        "SELECT id FROM postings WHERE final_score IS NOT NULL").fetchall()}
    assert done == {"b0", "b1"}

    # the full worklist: the two finished rows are cache hits, not new work
    allrows = conn.execute(
        core_db._POSTING_SELECT + "WHERE p.id LIKE 'b%' ORDER BY p.id").fetchall()
    llm2 = FakeBatchLlm(_batch_reply)
    stats = llm_bulk.score_postings_batch(conn, allrows, PROFILE, llm2, batch_size=2)
    assert stats["cached"] == 2
    assert stats["scored"] == 2
    assert llm2.calls == 1, "only the 2 unscored rows should need a call"


def test_batch_parse_accepts_bare_list_and_fences():
    items = [{"id": "a", "fit": 10}, {"id": "b", "fit": 20}]
    assert set(llm_bulk._parse_batch(json.dumps(items), ["a", "b"])) == {"a", "b"}
    fenced = "```json\n" + json.dumps({"results": items}) + "\n```"
    assert set(llm_bulk._parse_batch(fenced, ["a", "b"])) == {"a", "b"}


def test_batch_parse_falls_back_to_positional():
    """Ids absent or wrong: same length means same order."""
    out = llm_bulk._parse_batch(json.dumps({"results": [{"fit": 10}, {"fit": 20}]}),
                               ["x", "y"])
    assert out["x"]["fit"] == 10 and out["y"]["fit"] == 20


def test_batch_parse_rejects_unusable():
    assert llm_bulk._parse_batch("not json", ["a"]) is None
    assert llm_bulk._parse_batch(json.dumps({"nope": 1}), ["a"]) is None
    # nothing parseable at all
    assert llm_bulk._parse_batch(json.dumps({"results": [{"fit": "high"}]}), ["a"]) is None
    # ids we never asked for are dropped, not misapplied
    assert llm_bulk._parse_batch(json.dumps({"results": [{"id": "zz", "fit": 1}]}),
                                 ["a"]) is None


def test_batch_parse_salvages_a_short_reply():
    """A reply covering only some postings keeps those and drops the rest."""
    out = llm_bulk._parse_batch(
        json.dumps({"results": [{"id": "a", "fit": 1}, {"id": "b", "fit": 2}]}),
        ["a", "b", "c"])
    assert set(out) == {"a", "b"}, "c is absent so the caller falls back for it"


def test_batch_falls_back_to_single_on_unusable_reply(conn):
    rows = _seed(conn, 3)
    llm = FakeBatchLlm("total garbage")

    real = llm_bulk.score_posting

    def single(row, profile, client, c):
        return real(row, profile, FakeBatchLlm(CANNED), c)

    llm_bulk.score_posting = single
    try:
        stats = llm_bulk.score_postings_batch(conn, rows, PROFILE, llm, batch_size=3)
    finally:
        llm_bulk.score_posting = real

    assert stats["scored"] == 3, "rows must not be lost when a batch reply is unusable"
    assert stats["fallback"] == 3


def test_batch_falls_back_for_only_the_missing_rows(conn):
    """A short reply falls back for just the rows it omitted."""
    rows = _seed(conn, 3)
    llm = FakeBatchLlm(json.dumps({"results": [{"id": "b0", "fit": 55}]}))

    real = llm_bulk.score_posting

    def single(row, profile, client, c):
        return real(row, profile, FakeBatchLlm(CANNED), c)

    llm_bulk.score_posting = single
    try:
        stats = llm_bulk.score_postings_batch(conn, rows, PROFILE, llm, batch_size=3)
    finally:
        llm_bulk.score_posting = real

    assert stats["scored"] == 3
    assert stats["fallback"] == 2
    assert llm.calls == 1
