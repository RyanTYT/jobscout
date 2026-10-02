"""Tests for the Profile resume.md editor and the parse-into-YAML button."""

from __future__ import annotations

import pytest

RESUME_MD = """# Ada Lovelace

## Experience

### Acme — Quant Developer (2023-05 → present)
- built the thing

## Education

### BSc, Somewhere (2019)
"""

# a minimal resume that satisfies MasterResume
GOOD_YAML = """
identity:
  full_name: Ada Lovelace
  headline: Quant Developer
experience:
  - id: EXP1
    company: Acme
    role: Quant Developer
    dates:
      start: '2023-05'
    bullets:
      - id: B1
        text: built the thing
education: []
projects: []
skills:
  core: []
  certifications: []
  publications: []
"""


@pytest.fixture()
def resume_home(monkeypatch, tmp_path):
    """Relocate the resume dir so resume.md/.yaml land in tmp.

    Must patch the *importing* modules, not jobscout.core.paths: the stores do
    `from jobscout.core.paths import master_resume_dir`, so rebinding the
    attribute on paths does not move their captured reference — patching only
    paths silently writes to the real master_resume/resume.yaml.
    """
    from jobscout.core import paths as core_paths
    from jobscout.core import resume as core_resume
    from jobscout.core import resume_parser
    from jobscout.webapp.stores import profile_store

    d = tmp_path / "master_resume"
    d.mkdir()
    # resume_parser reaches the dir through paths.*; the stores hold their own
    # captured reference. Patch every seam or the real resume files get written.
    for mod in (core_paths, profile_store, core_resume, resume_parser):
        monkeypatch.setattr(mod, "master_resume_dir", lambda: d, raising=False)
    return d


def test_profile_page_has_the_markdown_card(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert 'name="resume_md"' in r.text
    assert "parse into YAML" in r.text
    assert "save markdown" in r.text


def test_markdown_saves_verbatim(client, resume_home):
    r = client.post("/profile/resume-md", data={"resume_md": RESUME_MD},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profile?md_saved=1"

    from jobscout.core.resume_parser import load_master_resume

    assert load_master_resume() == RESUME_MD


def test_markdown_round_trips_into_the_page(client, resume_home):
    client.post("/profile/resume-md", data={"resume_md": RESUME_MD},
                follow_redirects=False)
    r = client.get("/profile")
    assert "Ada Lovelace" in r.text
    assert "Quant Developer" in r.text


def test_markdown_save_keeps_a_backup(client, resume_home):
    client.post("/profile/resume-md", data={"resume_md": "first\n"},
                follow_redirects=False)
    client.post("/profile/resume-md", data={"resume_md": "second\n"},
                follow_redirects=False)
    from jobscout.core.resume_parser import master_resume_path

    bak = master_resume_path().with_suffix(".md.bak")
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == "first\n"


def test_empty_markdown_is_allowed(client, resume_home):
    """A scratchpad must survive an empty save — no LLM, no validation."""
    client.post("/profile/resume-md", data={"resume_md": RESUME_MD},
                follow_redirects=False)
    r = client.post("/profile/resume-md", data={"resume_md": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    from jobscout.core.resume_parser import load_master_resume

    assert load_master_resume() == ""


# ── parse into YAML ───────────────────────────────────────────────────────────


def _stub_parse(monkeypatch, result):
    from jobscout.core import resume_parser

    monkeypatch.setattr(resume_parser, "parse_resume", lambda text=None, **kw: result)
    # the router imports the symbol inside the handler, so patch the module
    return resume_parser


def test_parse_writes_validated_yaml(client, resume_home, monkeypatch):
    import yaml

    parsed = yaml.safe_load(GOOD_YAML)
    _stub_parse(monkeypatch, {"parsed": parsed, "model": "m", "cost": 0.0})

    r = client.post("/profile/parse-resume", data={"resume_md": RESUME_MD},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "parsed=1" in r.headers["location"]

    from jobscout.core.resume import load_master_resume

    saved = load_master_resume()
    assert saved.identity.full_name == "Ada Lovelace"
    assert saved.experience[0].company == "Acme"


def test_parse_saves_the_markdown_it_parsed(client, resume_home, monkeypatch):
    import yaml

    _stub_parse(monkeypatch, {"parsed": yaml.safe_load(GOOD_YAML)})
    client.post("/profile/parse-resume", data={"resume_md": RESUME_MD},
                follow_redirects=False)

    from jobscout.core.resume_parser import load_master_resume

    assert load_master_resume() == RESUME_MD, (
        "what was parsed must be what is on disk")


def test_parse_failure_leaves_yaml_untouched(client, resume_home, monkeypatch):
    from jobscout.core.resume import load_master_resume

    _stub_parse(monkeypatch, {"parsed": {"identity": {"full_name": "Real"}}})
    client.post("/profile/parse-resume", data={"resume_md": "v1\n"},
                follow_redirects=False)
    before = load_master_resume().identity.full_name

    _stub_parse(monkeypatch, {"error": "LLM not available",
                              "raw": "the model said no"})
    r = client.post("/profile/parse-resume", data={"resume_md": "v2\n"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert "parse_error=" in r.headers["location"]
    assert "parse_raw=" in r.headers["location"], "show the model output"
    assert load_master_resume().identity.full_name == before, (
        "a failed parse must not touch resume.yaml")


def test_parse_failure_still_saves_the_markdown(client, resume_home, monkeypatch):
    _stub_parse(monkeypatch, {"error": "boom"})
    client.post("/profile/parse-resume", data={"resume_md": "draft\n"},
                follow_redirects=False)

    from jobscout.core.resume_parser import load_master_resume

    assert load_master_resume() == "draft\n"


def test_parse_rejects_yaml_the_schema_refuses(client, resume_home, monkeypatch):
    """Output the schema refuses is a failure, not a write."""
    _stub_parse(monkeypatch, {"parsed": {"identity": {"full_name": ["a", "b"]}}})
    r = client.post("/profile/parse-resume", data={"resume_md": "x\n"},
                    follow_redirects=False)
    assert "parse_error=" in r.headers["location"]

    from jobscout.core.paths import master_resume_dir

    assert not (master_resume_dir() / "resume.yaml").exists()


def test_parse_will_not_wipe_the_resume_with_an_empty_one(
        client, resume_home, monkeypatch):
    """Every MasterResume field is optional, so `{}` passes the schema. It must
    still be refused — this is the data-loss case."""
    import yaml

    _stub_parse(monkeypatch, {"parsed": yaml.safe_load(GOOD_YAML)})
    client.post("/profile/parse-resume", data={"resume_md": "real\n"},
                follow_redirects=False)
    from jobscout.core.resume import load_master_resume

    before = load_master_resume().identity.full_name

    _stub_parse(monkeypatch, {"parsed": {"identity": {}}})
    r = client.post("/profile/parse-resume", data={"resume_md": "truncated\n"},
                    follow_redirects=False)

    assert "parse_error=" in r.headers["location"]
    assert load_master_resume().identity.full_name == before


def test_upload_resume_also_refuses_an_empty_file(resume_home):
    """The guard lives at the single write path, so the upload route inherits it."""
    from jobscout.webapp.stores import profile_store

    with pytest.raises(profile_store.ProfileError, match="empty"):
        profile_store.upload_resume("{}\n")
