"""Master resume loading, validation, and the canonical application field map
(PLAN §5.1–§5.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import yaml

from jobscout.core.paths import master_resume_dir
from jobscout.core.schema import MasterResume


class ResumeError(Exception):
    pass


@dataclass
class Issue:
    level: str  # "error" | "warn"
    msg: str


def load_master_resume() -> MasterResume:
    path = master_resume_dir() / "resume.yaml"
    if not path.is_file():
        raise ResumeError(f"missing {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ResumeError(f"invalid YAML in {path}: {e}") from e
    try:
        return MasterResume.model_validate(data)
    except Exception as e:
        raise ResumeError(f"resume.yaml failed schema validation: {e}") from e


# ── Referential integrity (PLAN §5.2) ────────────────────────────────────────


def _collect_ids(resume: MasterResume) -> dict[str, str]:
    """id -> human description, for every stable ID in the resume."""
    ids: dict[str, str] = {}
    for link in resume.identity.links.extra:
        ids[link.id] = f"link:{link.label}"
    for edu in resume.education:
        ids[edu.id] = f"education:{edu.school}"
    for exp in resume.experience:
        ids[exp.id] = f"experience:{exp.company}"
        for b in exp.bullets:
            ids[b.id] = f"bullet@{exp.id}: {b.text[:60]}"
    for proj in resume.projects:
        ids[proj.id] = f"project:{proj.name}"
        for b in proj.bullets:
            ids[b.id] = f"bullet@{proj.id}: {b.text[:60]}"
    for pub in resume.skills.publications:
        ids[pub.id] = f"publication:{pub.title[:60]}"
    for other in resume.extras.other:
        ids[other.id] = f"extras:{other.label}"
    return ids


_NARRATIVE_REFS = re.compile(r"\(refs:\s*([^)]+)\)")


def _narrative_refs() -> list[tuple[int, str]]:
    path = master_resume_dir() / "narrative.md"
    if not path.is_file():
        return []
    refs: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("    ") or line.startswith("\t"):
            continue  # indented = code block (templates/examples) - not real refs
        m = _NARRATIVE_REFS.search(line)
        if m:
            for token in m.group(1).split(","):
                token = token.strip()
                if token:
                    refs.append((i, token))
    return refs


def validate_resume() -> list[Issue]:
    """Structural + referential checks. Errors block packet generation (P6)."""
    resume = load_master_resume()  # raises ResumeError on schema failure
    issues: list[Issue] = []
    ids = _collect_ids(resume)

    # unique IDs
    seen: dict[str, int] = {}
    for id_ in ids:
        seen[id_] = seen.get(id_, 0) + 1
    for id_, count in seen.items():
        if count > 1:
            issues.append(Issue("error", f"duplicate ID {id_} (appears {count}×) — IDs must be unique and stable"))

    # narrative refs resolve to real IDs
    for line_no, token in _narrative_refs():
        if token not in ids:
            issues.append(Issue("error", f"narrative.md:{line_no} refs unknown ID {token!r}"))

    # at most one current employment
    current = [e for e in resume.experience if e.dates.is_current]
    if len(current) > 1:
        names = ", ".join(e.company for e in current)
        issues.append(Issue("warn", f"multiple current employers ({names}) — check dates.end"))

    # skeleton / placeholder detection
    placeholders = [
        v
        for v in (resume.identity.full_name, resume.identity.email)
        if v in (None, "", "…")
    ]
    if placeholders or not resume.experience:
        issues.append(Issue("warn", "master resume is still the skeleton — fill it in before packets (P6)"))

    if not resume.identity.links.personal_website:
        issues.append(Issue("warn", "identity.links.personal_website is empty (owner wants the personal site listed)"))

    return issues


# ── Canonical application field map (PLAN §5.3) ───────────────────────────────


@dataclass
class FieldEntry:
    canonical_key: str
    value: Any
    source: str
    note: str = ""


def _years_experience(resume: MasterResume) -> int | None:
    starts = [
        e.dates.start
        for e in resume.experience
        if e.dates and e.dates.start
    ]
    if not starts:
        return None
    earliest = min(starts)
    y, m = int(earliest[:4]), int(earliest[5:7])
    months = (date.today().year - y) * 12 + (date.today().month - m)
    return max(0, months // 12)


def _current_experience(resume: MasterResume):
    current = [e for e in resume.experience if e.dates.is_current]
    if current:
        return max(current, key=lambda e: e.dates.start)
    if resume.experience:
        return max(resume.experience, key=lambda e: e.dates.start)
    return None


def _last_education(resume: MasterResume):
    return resume.education[-1] if resume.education else None


def field_map(resume: MasterResume | None = None) -> list[FieldEntry]:
    """The canonical key → value map every application draws from (§5.3).
    Derived fields (years_experience, current_*) are computed, never stored."""
    if resume is None:
        resume = load_master_resume()
    r = resume
    cur = _current_experience(r)
    edu = _last_education(r)
    wa = r.work_authorization[0] if r.work_authorization else None

    entries = [
        FieldEntry("full_name", r.identity.full_name, "identity.full_name"),
        FieldEntry("preferred_name", r.identity.preferred_name, "identity.preferred_name"),
        FieldEntry("email", r.identity.email, "identity.email"),
        FieldEntry("phone", r.identity.phone, "identity.phone"),
        FieldEntry("city", r.identity.location.city, "identity.location.city"),
        FieldEntry("region", r.identity.location.region, "identity.location.region"),
        FieldEntry("country", r.identity.location.country, "identity.location.country"),
        FieldEntry("full_address", None, "identity.location", "only if form requires + policy allows"),
        FieldEntry("linkedin", r.identity.links.linkedin, "identity.links.linkedin"),
        FieldEntry("github", r.identity.links.github, "identity.links.github"),
        FieldEntry("personal_website", r.identity.links.personal_website, "identity.links.personal_website"),
        FieldEntry(
            "current_company", cur.company if cur else None,
            "derived: latest experience (end=present)",
        ),
        FieldEntry(
            "current_title", cur.title if cur else None,
            "derived: latest experience (end=present)",
        ),
        FieldEntry("years_experience", _years_experience(r), "derived: earliest dates.start"),
        FieldEntry("highest_degree", edu.degree if edu else None, "derived: education[-1]"),
        FieldEntry("school", edu.school if edu else None, "education[-1].school"),
        FieldEntry("field", edu.field if edu else None, "education[-1].field"),
        FieldEntry("graduation_year", (edu.dates.end[:4] if edu and edu.dates and edu.dates.end and edu.dates.end != "present" else None), "education[-1].dates.end"),
        FieldEntry("gpa", edu.gpa if edu else None, "education[-1].gpa", "optional per policy"),
        FieldEntry("work_authorization", wa.status if wa else None, "work_authorization[0].status"),
        FieldEntry("sponsorship_needed", wa.sponsorship_needed if wa else None, "work_authorization[0].sponsorship_needed"),
        FieldEntry("salary_expectation", r.extras.salary.policy, "extras.salary.policy", "policy value"),
        FieldEntry("notice_period", r.extras.notice_period, "extras.notice_period"),
        FieldEntry("earliest_start", r.extras.availability, "extras.availability"),
        FieldEntry("referral_source", r.extras.referrals.policy, "extras.referrals.policy", "honest default"),
        FieldEntry("resume_file", None, "packet (tailored PDF)", "generated per posting (P6)"),
        FieldEntry("cover_letter", None, "packet (generated text)", "generated per posting (P6)"),
        FieldEntry("eeo:*", r.extras.eeo.policy, "extras.eeo.policy", "declined by default"),
    ]
    for link in r.identity.links.extra:
        entries.append(FieldEntry(f"link:{link.label}", link.url, f"identity.links.extra[{link.id}]"))
    for other in r.extras.other:
        entries.append(FieldEntry(f"other:{other.label}", other.value, f"extras.other[{other.id}]"))
    return entries


# ── the profile detail dump ──────────────────────────────────────────────────
# resume.yaml is the claim-checked, structured truth: short, stable IDs, and
# nothing that is not defensible in an interview. It is also lossy — it cannot
# hold the reasoning behind a claim, the story behind a project, the warnings
# about what NOT to say, or the open questions.
#
# detail.md is the other half: a long-form, free-form dump of everything known
# about the candidate (positioning guide, per-role "lead with" tables, the
# quantitative-claims ledger with [V]/[EST]/[?] markers, the story bank, open
# items). It is never parsed into resume.yaml and never claim-checked — it is
# context for judgment calls: which companies fit, how to pitch, what to avoid
# asserting. Harnesses read it; the packet pipeline does not.


def detail_dump_path():
    """master_resume/detail.md — the free-form profile dump."""
    return master_resume_dir() / "detail.md"


def detail_dump() -> str:
    """The raw detail dump, or "" when absent. Never raises."""
    p = detail_dump_path()
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def detail_dump_stats() -> dict:
    """Shape of the dump, for the Profile page header."""
    text = detail_dump()
    return {
        "path": str(detail_dump_path()),
        "exists": bool(text.strip()),
        "chars": len(text),
        "lines": len(text.splitlines()) if text else 0,
        "sections": [
            ln.lstrip("# ").strip()
            for ln in text.splitlines()
            if ln.startswith("## ")
        ],
    }


def detail_dump_for_prompt(max_chars: int = 14000) -> str:
    """The dump, framed for an LLM prompt and bounded.

    A full dump runs to tens of thousands of characters; the harnesses get one
    per run, so a cap keeps the call affordable. Truncation is announced rather
    than silent, and the head is kept because the positioning guide and the
    claims ledger live at the top — the parts that change a judgment call.
    """
    text = detail_dump().strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    kept = text[:max_chars]
    # prefer to cut at a section boundary so the text does not end mid-table
    cut = kept.rfind("\n## ")
    if cut > max_chars // 2:
        kept = kept[:cut]
    return (
        f"{kept}\n\n[detail dump truncated at {len(kept)} of {len(text)} chars — "
        f"the remainder covers later sections (story bank, open items). Treat "
        f"what is above as the authoritative positioning and claims guidance.]"
    )
