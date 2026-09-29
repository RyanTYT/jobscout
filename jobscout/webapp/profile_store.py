"""webapp/profile_store.py — UI editor for the master resume (PLAN §5.1).

The master resume is the single source of truth for every application
field, so this store writes master_resume/resume.yaml directly. Edits are
line-level textual patches (the set_discovery_mode convention) so the
file's extensive comments survive every save. A save that would break
schema validation is rejected — the file is restored and the error shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jobscout.core import resume as core_resume
from jobscout.core.paths import master_resume_dir


class ProfileError(Exception):
    pass


# (form key, yaml line matcher, label, placeholder, kind)
@dataclass
class ProfileField:
    key: str
    label: str
    line_key: str          # the YAML key on its line, e.g. "full_name:"
    block: str             # enclosing top-level block, e.g. "identity"
    placeholder: str = ""
    kind: str = "text"     # text | select
    options: tuple = ()

    @property
    def input_name(self) -> str:
        return f"f_{self.key}"


FIELDS: list[ProfileField] = [
    # identity
    ProfileField("full_name", "Full name", "full_name:", "identity",
                 "Jane Doe"),
    ProfileField("preferred_name", "Preferred name", "preferred_name:",
                 "identity", "Jane"),
    ProfileField("email", "Email", "email:", "identity", "you@example.com"),
    ProfileField("phone", "Phone", "phone:", "identity", "+1 555 000 0000"),
    ProfileField("city", "City", "city:", "identity", "Singapore"),
    ProfileField("region", "Region / state", "region:", "identity",
                 "e.g. California"),
    ProfileField("country", "Country", "country:", "identity", "Singapore"),
    ProfileField("linkedin", "LinkedIn", "linkedin:", "identity",
                 "https://linkedin.com/in/…"),
    ProfileField("github", "GitHub", "github:", "identity",
                 "https://github.com/…"),
    ProfileField("personal_website", "Personal website",
                 "personal_website:", "identity", "https://…"),
    # current job (drives current_company / current_title / years math)
    ProfileField("current_company", "Current company", None, "experience",
                 "Acme Trading"),
    ProfileField("current_title", "Current title", None, "experience",
                 "Senior Software Engineer"),
    ProfileField("employment_start", "Employed since (YYYY-MM, feeds "
                 "years-of-experience)", None, "experience", "2019-06"),
    # education (highest degree block)
    ProfileField("school", "School", None, "education", "…"),
    ProfileField("degree", "Highest degree", None, "education", "BS"),
    ProfileField("field", "Field of study", None, "education",
                 "Computer Science"),
    ProfileField("graduation_year", "Graduation year (YYYY)", None,
                 "education", "2019"),
    # work authorization
    ProfileField("wa_country", "Work authorization country", None,
                 "work_authorization", "US"),
    ProfileField("wa_status", "Work authorization status", None,
                 "work_authorization", "citizen",
                 kind="select",
                 options=("citizen", "permanent-resident", "visa-holder",
                          "other")),
    # extras that forms commonly ask
    ProfileField("notice_period", "Notice period", "notice_period:", "extras",
                 "2 weeks"),
    ProfileField("availability", "Earliest start", "availability:", "extras",
                 "immediate"),
]


def _resume_path() -> Path:
    return master_resume_dir() / "resume.yaml"


def upload_resume(text: str) -> dict:
    """Replace master_resume/resume.yaml wholesale with a validated upload.

    The incoming YAML must parse into the MasterResume schema — anything
    else is rejected with the file untouched. The previous resume is kept
    as resume.yaml.bak alongside (one rolling backup). Returns a summary
    for the flash."""
    import yaml

    from jobscout.core.models import MasterResume
    from jobscout.core.resume import load_master_resume

    path = _resume_path()
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError as e:
        raise ProfileError(f"not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError("expected a YAML mapping at the top level")
    try:
        MasterResume.model_validate(data)
    except Exception as e:                       # noqa: BLE001 — pydantic detail
        raise ProfileError(f"resume schema rejected it: {e}") from e

    if path.is_file():
        backup = path.with_suffix(".yaml.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    resume = load_master_resume()
    return {"fields": sum(
        1 for e in resume.experience or []) + len(resume.education or []),
            "backup": str(path.with_suffix(".yaml.bak"))}


def current_values() -> dict:
    """Form-friendly view of the current resume values."""
    try:
        r = core_resume.load_master_resume()
    except core_resume.ResumeError:
        return {f.key: "" for f in FIELDS}
    cur = (r.experience[-1] if r.experience else None)
    edu = (r.education[-1] if r.education else None)
    wa = (r.work_authorization[0] if r.work_authorization else None)
    vals = {
        "full_name": r.identity.full_name,
        "preferred_name": r.identity.preferred_name,
        "email": r.identity.email,
        "phone": r.identity.phone,
        "city": r.identity.location.city,
        "region": r.identity.location.region,
        "country": r.identity.location.country,
        "linkedin": r.identity.links.linkedin,
        "github": r.identity.links.github,
        "personal_website": r.identity.links.personal_website,
        "current_company": cur.company if cur else None,
        "current_title": cur.title if cur else None,
        "employment_start": (cur.dates.start if cur and cur.dates else None),
        "school": edu.school if edu else None,
        "degree": edu.degree if edu else None,
        "field": edu.field if edu else None,
        "graduation_year": (edu.dates.end[:4] if edu and edu.dates and
                            edu.dates.end and edu.dates.end != "present"
                            else None),
        "wa_country": wa.country if wa else None,
        "wa_status": wa.status if wa else None,
        "notice_period": r.extras.notice_period,
        "availability": r.extras.availability,
    }
    out = {}
    for f in FIELDS:
        v = vals.get(f.key)
        out[f.key] = "" if v in (None, "…") else str(v)
    return out


def missing_required() -> list[str]:
    """Labels of required fields still empty — the required set is the
    SAME registry the fill sheet uses (packets/field_map.CHECKLIST), so
    the profile editor and packet readiness can never disagree."""
    from jobscout.packets.field_map import CHECKLIST

    vals = current_values()
    by_key = {f.key: f for f in FIELDS}
    out = []
    for _label, key, required in CHECKLIST:
        f = by_key.get(key)
        if required and f and not vals.get(key):
            out.append(f.label)
    return out


# ── writing (textual patches, comments preserved) ────────────────────────────

def _yaml_str(v: str) -> str:
    v = (v or "").strip()
    return f'"{v}"' if v else "null"


def _set_line(lines: list[str], key: str, value: str) -> bool:
    """Replace `key: <anything>` with `key: <quoted value>` (first match)."""
    pat = re.compile(rf"^(\s*){re.escape(key)}\s*[:?]?[^\n]*$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m and not line.lstrip().startswith("#"):
            lines[i] = f"{m.group(1)}{key} {_yaml_str(value)}"
            return True
    return False


def _set_experience(lines: list[str], company: str, title: str,
                    start: str) -> bool:
    """Create/replace the CURRENT experience entry block."""
    # if a real experience block already exists, patch its head fields
    for i, line in enumerate(lines):
        if line.startswith("experience:"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip().startswith("- id:"):
                # patch company/title/dates lines within this first entry
                for j in range(i + 1, min(i + 40, len(lines))):
                    if lines[j] and not lines[j][0].isspace():
                        break
                    s = lines[j].strip()
                    if s.startswith("company:"):
                        lines[j] = re.sub(r"company:.*",
                                          f"company: {_yaml_str(company)}",
                                          lines[j])
                    elif s.startswith("title:"):
                        lines[j] = re.sub(r"title:.*",
                                          f"title: {_yaml_str(title)}",
                                          lines[j])
                    elif s.startswith("start:"):
                        lines[j] = re.sub(r"start:.*",
                                          f'start: "{start}"', lines[j])
                return True
            # empty list — replace with a real block
            block = [
                "experience:",
                "- id: EXPCUR                    # current role (profile editor)",
                f"  company: {_yaml_str(company)}",
                '  domain: ""',
                '  context: "current role"',
                f"  title: {_yaml_str(title)}",
                "  titles: []",
                f'  dates: {{start: "{start}", end: present}}',
                '  location: ""',
                "  employment: full-time",
                "  tech: []",
                "  bullets: []",
            ]
            lines[i:i + 1] = block
            return True
    return False


def _set_education(lines: list[str], school: str, degree: str, field: str,
                   year: str) -> bool:
    for i, line in enumerate(lines):
        if line.startswith("education:"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip().startswith("- id:"):
                for j in range(i + 1, min(i + 30, len(lines))):
                    if lines[j] and not lines[j][0].isspace():
                        break
                    s = lines[j].strip()
                    if s.startswith("school:"):
                        lines[j] = re.sub(r"school:.*",
                                          f"school: {_yaml_str(school)}",
                                          lines[j])
                    elif s.startswith("degree:"):
                        lines[j] = re.sub(r"degree:.*",
                                          f"degree: {_yaml_str(degree)}",
                                          lines[j])
                    elif s.startswith("field:"):
                        lines[j] = re.sub(r"field:.*",
                                          f"field: {_yaml_str(field)}",
                                          lines[j])
                    elif s.startswith("end:") and year:
                        lines[j] = re.sub(r"end:.*", f'end: "{year}-05"',
                                          lines[j])
                return True
            block = [
                "education:",
                "- id: EDUCUR                    # highest degree (profile editor)",
                f"  school: {_yaml_str(school)}",
                f"  degree: {_yaml_str(degree)}",
                f"  field: {_yaml_str(field)}",
                f'  dates: {{start: "", end: "{year}-05"}}'
                if year else '  dates: {start: "", end: null}',
                "  gpa: null",
                "  honors: []",
                "  highlights: []",
            ]
            lines[i:i + 1] = block
            return True
    return False


def _set_work_auth(lines: list[str], country: str, status: str) -> bool:
    for i, line in enumerate(lines):
        if line.startswith("work_authorization:"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip().startswith("- country:"):
                for j in range(i + 1, min(i + 10, len(lines))):
                    if lines[j] and not lines[j][0].isspace():
                        break
                    s = lines[j].strip()
                    if s.startswith("country:"):
                        lines[j] = re.sub(r"country:.*",
                                          f"country: {_yaml_str(country)}",
                                          lines[j])
                    elif s.startswith("status:"):
                        repl = status or "citizen"
                        lines[j] = re.sub(
                            r"status:.*", f"status: {repl}", lines[j])
                return True
            block = [
                "work_authorization:",
                f"- country: {_yaml_str(country)}",
                f'  status: "{status or "citizen"}"',
                "  sponsorship_needed: false",
                '  note: ""',
            ]
            lines[i:i + 1] = block
            return True
    return False


def _set_custom_fields(lines: list[str], entries: list[tuple[str, str]]) -> bool:
    """Rewrite the extras.other list from (label, value) pairs."""
    for i, line in enumerate(lines):
        if line.strip() == "other: []" and "other:" in line:
            if not entries:
                return True
            block = ["other:"]
            for n, (label, value) in enumerate(entries, 1):
                lid = re.sub(r"[^A-Za-z0-9]+", "", label)[:10].upper() or "X"
                block.append(f"  - id: X{lid}{n}")
                block.append(f"    label: {_yaml_str(label)}")
                block.append(f"    value: {_yaml_str(value)}")
            lines[i:i + 1] = block
            return True
    # fall back: append under extras block
    for i, line in enumerate(lines):
        if line.startswith("extras:"):
            insert = ["  other: []"]
            if entries:
                insert = ["  other:"]
                for n, (label, value) in enumerate(entries, 1):
                    lid = re.sub(r"[^A-Za-z0-9]+", "", label)[:10].upper() or "X"
                    insert.append(f"  - id: X{lid}{n}")
                    insert.append(f"    label: {_yaml_str(label)}")
                    insert.append(f"    value: {_yaml_str(value)}")
            lines[i + 1:i + 1] = insert
            return True
    return False


def _bump_version(lines: list[str]) -> None:
    for i, line in enumerate(lines):
        m = re.match(r"^version:\s*(\d+)\s*$", line)
        if m:
            lines[i] = f"version: {int(m.group(1)) + 1}"
            return


def save_profile(form: dict) -> None:
    """Write form values into resume.yaml. Raises ProfileError (file intact)
    if the result fails schema validation."""
    path = _resume_path()
    if not path.is_file():
        raise ProfileError(f"missing {path}")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()

    val = lambda k: (form.get(f"f_{k}") or "").strip()  # noqa: E731

    # scalar identity/extras fields
    for f in FIELDS:
        if f.line_key:
            _set_line(lines, f.line_key, val(f.key))

    # structured sections (only when the form provides values)
    if val("current_company") or val("current_title"):
        _set_experience(lines, val("current_company"),
                        val("current_title"),
                        val("employment_start") or "2000-01")
    if val("school") or val("degree"):
        _set_education(lines, val("school"), val("degree"),
                       val("field"), val("graduation_year"))
    if val("wa_country") or val("wa_status"):
        _set_work_auth(lines, val("wa_country"),
                       val("wa_status") or "citizen")

    # custom fields: repeated label/value pairs from the form
    custom: list[tuple[str, str]] = []
    for i in range(1, 30):
        label = (form.get(f"cf_label_{i}") or "").strip()
        value = (form.get(f"cf_value_{i}") or "").strip()
        if label:
            custom.append((label, value))
    if custom or form.get("cf_touched") == "1":
        _set_custom_fields(lines, custom)

    _bump_version(lines)
    new_text = "\n".join(lines) + "\n"

    path.write_text(new_text, encoding="utf-8")
    try:
        core_resume.load_master_resume()
    except core_resume.ResumeError as e:
        path.write_text(original, encoding="utf-8")   # rollback
        raise ProfileError(
            f"save rejected — resume.yaml would become invalid: {e}"
        ) from e


def custom_fields() -> list[dict]:
    """Current extras.other entries as label/value rows."""
    try:
        r = core_resume.load_master_resume()
    except core_resume.ResumeError:
        return []
    return [{"label": o.label, "value": o.value or ""} for o in r.extras.other]
