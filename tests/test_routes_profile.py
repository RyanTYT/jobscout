"""test_routes_profile — route tests split from the old test_webapp god-file
(one file per router area; shared fixtures in conftest.py)"""

from __future__ import annotations

from pathlib import Path

VALID_RESUME_YAML = """identity:
  full_name: Upload Tester
  email: upload@example.com
experience:
  - id: EXPCUR
    company: Uploaded Co
    title: Engineer
    dates: {start: "2024-01"}
education:
  - id: EDU1
    school: Upload U
    degree: BS
    dates: {start: "2016-09", end: "2020-06"}
"""


def test_profile_page_renders(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Your profile" in r.text
    assert "Full name" in r.text
    assert "Custom fields" in r.text



def test_profile_save_roundtrip(client, monkeypatch):
    import shutil
    import tempfile

    from jobscout.webapp.stores import profile_store
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.stores.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    r = client.post("/profile/save", data={
        "f_full_name": "Jane Doe",
        "f_email": "jane@example.com",
        "f_current_company": "Acme Trading",
        "f_current_title": "Senior Software Engineer",
        "f_employment_start": "2019-06",
        "f_school": "NUS",
        "f_degree": "BS",
        "cf_touched": "1",
        "cf_label_1": "portfolio",
        "cf_value_1": "https://janedoe.dev",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profile?saved=1"

    text = (tmp / "resume.yaml").read_text()
    assert 'full_name: "Jane Doe"' in text
    assert "Acme Trading" in text
    assert "comments are preserved" or "#" in text  # comments intact
    # comments from the original file survived
    assert "THE source of truth" in text

    values = profile_store.current_values()
    assert values["full_name"] == "Jane Doe"
    assert values["current_company"] == "Acme Trading"

    import shutil as sh
    sh.rmtree(tmp.parent)



def test_profile_save_invalid_rolls_back(client, monkeypatch):
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "master_resume"
    tmp.mkdir()
    shutil.copytree(core_resume_dir(), tmp, dirs_exist_ok=True)
    monkeypatch.setattr(
        "jobscout.webapp.stores.profile_store.master_resume_dir", lambda: tmp)
    monkeypatch.setattr(
        "jobscout.core.resume.master_resume_dir", lambda: tmp)

    before = (tmp / "resume.yaml").read_text()
    # force validation failure by monkeypatching the validator
    import jobscout.core.resume as cr
    orig = cr.load_master_resume

    def broken():
        raise cr.ResumeError("boom")
    monkeypatch.setattr(cr, "load_master_resume", broken)
    monkeypatch.setattr(
        "jobscout.webapp.stores.profile_store.core_resume.load_master_resume", broken)

    r = client.post("/profile/save", data={"f_full_name": "X"},
                    follow_redirects=False)
    assert r.status_code == 422
    assert (tmp / "resume.yaml").read_text() == before
    monkeypatch.setattr(cr, "load_master_resume", orig)
    import shutil as sh
    sh.rmtree(tmp.parent)



def test_resume_upload_replaces_with_backup(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths
    from jobscout.core import resume as cr

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr(cr, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr("jobscout.webapp.stores.profile_store.master_resume_dir",
                        lambda: tmp_path)
    before = (tmp_path / "resume.yaml").read_text(encoding="utf-8")

    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.yaml", VALID_RESUME_YAML,
                                           "application/x-yaml")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "uploaded=1" in r.headers["location"]
    after = (tmp_path / "resume.yaml").read_text(encoding="utf-8")
    assert after == VALID_RESUME_YAML
    assert (tmp_path / "resume.yaml.bak").read_text(encoding="utf-8") == before
    # the form now reflects the uploaded values
    page = client.get("/profile")
    assert "Upload Tester" in page.text and "Uploaded Co" in page.text



def test_resume_upload_invalid_rejected_untouched(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths
    from jobscout.core import resume as cr

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr(cr, "master_resume_dir", lambda: tmp_path)
    monkeypatch.setattr("jobscout.webapp.stores.profile_store.master_resume_dir",
                        lambda: tmp_path)
    before = (tmp_path / "resume.yaml").read_text(encoding="utf-8")

    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.yaml", "not: [valid",
                                           "application/x-yaml")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "upload_error=" in r.headers["location"]
    assert (tmp_path / "resume.yaml").read_text(encoding="utf-8") == before



def test_resume_upload_rejects_non_yaml(client):
    r = client.post("/profile/upload-resume",
                    files={"resume_file": ("resume.pdf", b"%PDF-",
                                           "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "upload_error=" in r.headers["location"]



def test_resume_download_serves_file(client, monkeypatch, tmp_path):
    import shutil as _sh

    from jobscout.core import paths as core_paths

    real = core_paths.master_resume_dir()
    _sh.copytree(real, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(core_paths, "master_resume_dir", lambda: tmp_path)
    r = client.get("/profile/resume-download")
    assert r.status_code == 200
    assert "full_name" in r.text




def core_resume_dir():
    from jobscout.core.paths import master_resume_dir
    return master_resume_dir()
