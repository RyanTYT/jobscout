"""Tests for tier-1 bulk scoring: parse, cache, final-score math (P2)."""

from __future__ import annotations

import json
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
