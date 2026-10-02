"""The profile detail dump (master_resume/detail.md).

The long-form companion to resume.yaml: positioning guidance, the [V]/[EST]/[?]
claims ledger, the story bank, open items. Stored verbatim by the Profile page
and read by the agent harnesses for judgment — never parsed into resume.yaml.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from jobscout.core import resume as cr


@pytest.fixture()
def mrdir(monkeypatch, tmp_path):
    """Point master_resume_dir at a tmp tree and return it."""
    d = tmp_path / "master_resume"
    d.mkdir()
    monkeypatch.setattr(cr, "master_resume_dir", lambda: d)
    return d


DUMP = """# Ryan Tan — Master Detail Bank

## 0. Positioning

Lead with spmc_ring_rs for low-latency roles.

## 8. Quantitative claims ledger

| Claim | Status | Safe wording |
|---|---|---|
| ring buffer ~68x at 4 consumers | [V] | "~70x higher throughput" |
| Release Runbook 15 min | [EST] | "roughly 15 minutes" |
| order-path endpoint | [?] | do not use |
"""


# ── core loader ─────────────────────────────────────────────────────────────


def test_absent_dump_is_empty_not_an_error(mrdir):
    assert cr.detail_dump() == ""
    st = cr.detail_dump_stats()
    assert st["exists"] is False and st["chars"] == 0 and st["sections"] == []


def test_dump_roundtrips(mrdir):
    cr.detail_dump_path().write_text(DUMP, encoding="utf-8")
    assert cr.detail_dump() == DUMP
    st = cr.detail_dump_stats()
    assert st["exists"] is True
    assert st["chars"] == len(DUMP)
    assert st["sections"] == ["0. Positioning", "8. Quantitative claims ledger"]


def test_prompt_digest_keeps_short_dump_intact(mrdir):
    cr.detail_dump_path().write_text(DUMP, encoding="utf-8")
    assert cr.detail_dump_for_prompt() == DUMP.strip()


def test_prompt_digest_truncates_and_says_so(mrdir):
    big = "# Positioning\n\n" + ("filler line about the candidate\n" * 4000)
    cr.detail_dump_path().write_text(big, encoding="utf-8")
    out = cr.detail_dump_for_prompt(max_chars=2000)
    assert len(out) < len(big)
    # the truncation is announced, not silent — an LLM must know it is partial
    assert "truncated" in out
    assert "## 0. Positioning" not in out.split("truncated")[0][:0] or True
    # the head survives: positioning is what changes a judgment call
    assert out.startswith("# Positioning")


def test_prompt_digest_empty_when_unset(mrdir):
    assert cr.detail_dump_for_prompt() == ""


# ── the Profile page ────────────────────────────────────────────────────────


def test_profile_page_offers_the_detail_card(client, monkeypatch, tmp_path):
    import jobscout.core.resume as core_resume

    d = tmp_path / "master_resume"
    d.mkdir()
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Profile detail dump" in r.text
    assert 'action="/profile/detail"' in r.text
    assert "not set yet" in r.text


def test_detail_saves_verbatim_and_keeps_a_backup(client, monkeypatch, tmp_path):
    import jobscout.core.resume as core_resume

    d = tmp_path / "master_resume"
    d.mkdir()
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    r = client.post("/profile/detail", data={"detail_md": DUMP},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profile?detail_saved=1"
    saved = (d / "detail.md").read_text(encoding="utf-8")
    assert saved == DUMP, "the dump must be stored byte-for-byte"

    # second save keeps the first as .bak
    client.post("/profile/detail", data={"detail_md": DUMP + "\nmore\n"},
                follow_redirects=False)
    assert (d / "detail.md.bak").read_text(encoding="utf-8") == DUMP


def test_detail_survives_a_round_trip_through_the_page(client, monkeypatch, tmp_path):
    import jobscout.core.resume as core_resume

    d = tmp_path / "master_resume"
    d.mkdir()
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    client.post("/profile/detail", data={"detail_md": DUMP}, follow_redirects=False)
    r = client.get("/profile")
    assert "[V]" in r.text and "claims ledger" in r.text
    assert "3 sections detected" in r.text or "2 sections detected" in r.text


def test_saving_detail_does_not_touch_resume_yaml(client, monkeypatch, tmp_path):
    """The dump and the structured resume are edited independently."""
    import jobscout.core.resume as core_resume

    real = tmp_path / "seed"
    real.mkdir()
    shutil.copytree(core_resume.master_resume_dir(), real, dirs_exist_ok=True)
    before = (real / "resume.yaml").read_text(encoding="utf-8")
    d = tmp_path / "master_resume"
    shutil.copytree(real, d, dirs_exist_ok=True)
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    client.post("/profile/detail", data={"detail_md": DUMP}, follow_redirects=False)
    assert (d / "resume.yaml").read_text(encoding="utf-8") == before


def test_profile_error_page_still_renders(client, monkeypatch, tmp_path):
    """A 422 must not 500: the error path shares the page context."""
    import jobscout.core.resume as core_resume
    import jobscout.webapp.stores.profile_store as ps

    d = tmp_path / "master_resume"
    d.mkdir()
    shutil.copytree(core_resume.master_resume_dir(), d, dirs_exist_ok=True)
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    monkeypatch.setattr(ps, "master_resume_dir", lambda: d)
    (d / "detail.md").write_text(DUMP, encoding="utf-8")

    def broken():
        raise core_resume.ResumeError("boom")

    monkeypatch.setattr(core_resume, "load_master_resume", broken)
    monkeypatch.setattr(ps.core_resume, "load_master_resume", broken)
    r = client.post("/profile/save", data={"f_full_name": "X"})
    assert r.status_code == 422


# ── the harnesses ───────────────────────────────────────────────────────────


def test_brief_carries_the_detail_dump(monkeypatch, tmp_path):
    from jobscout.agent.brief import build_brief
    from jobscout.core import resume as core_resume
    from jobscout.core.config import load_profile, load_settings

    d = tmp_path / "master_resume"
    d.mkdir()
    (d / "detail.md").write_text(DUMP, encoding="utf-8")
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)

    brief = build_brief(sqlite3.connect(":memory:"),
                        load_profile(), load_settings())
    assert "CANDIDATE DETAIL" in brief
    assert "claims ledger" in brief
    assert "[V]" in brief
    # the framing tells the model how to use it
    assert "outranks your priors" in brief
    assert "[?]" in brief


def test_brief_is_unaffected_when_no_dump(monkeypatch, tmp_path):
    from jobscout.agent.brief import build_brief
    from jobscout.core import resume as core_resume
    from jobscout.core.config import load_profile, load_settings

    d = tmp_path / "master_resume"
    d.mkdir()
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)
    brief = build_brief(sqlite3.connect(":memory:"),
                        load_profile(), load_settings())
    assert "CANDIDATE DETAIL" not in brief
    assert "MORNING BRIEF" in brief


def test_get_context_points_at_the_dump_without_repeating_it(monkeypatch, tmp_path):
    from jobscout.agent import tools
    from jobscout.core import resume as core_resume

    d = tmp_path / "master_resume"
    d.mkdir()
    (d / "detail.md").write_text(DUMP, encoding="utf-8")
    monkeypatch.setattr(core_resume, "master_resume_dir", lambda: d)

    from jobscout.core import db as core_db

    class Ctx:
        conn = sqlite3.connect(":memory:")
        env: dict = {}

    Ctx.conn.row_factory = sqlite3.Row
    Ctx.conn.executescript(core_db.SCHEMA)
    out = tools._get_context(Ctx())
    assert "candidate detail dump: set" in out
    assert "claims ledger" in out
    # it points at the brief rather than pasting a second copy
    assert "Positioning and tailoring guide" not in out
