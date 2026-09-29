"""Fill sheet (PLAN §5.4) — the "list of values a job application needs".

Built from the canonical field map (core/resume.field_map) + a required-flag
checklist. The form-scan upgrade (exact ATS labels) lands in P7; this is the
checklist variant. Confidence: exact | derived | policy | missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobscout.core.resume import field_map
from jobscout.core.schema import MasterResume

# (label, canonical_key, required)
CHECKLIST: list[tuple[str, str, bool]] = [
    ("Full Name", "full_name", True),
    ("Preferred Name", "preferred_name", False),
    ("Email", "email", True),
    ("Phone", "phone", True),
    ("City", "city", False),
    ("Region / State", "region", False),
    ("Country", "country", True),
    ("LinkedIn", "linkedin", True),
    ("GitHub", "github", False),
    ("Personal Website", "personal_website", False),
    ("Current Company", "current_company", True),
    ("Current Title", "current_title", True),
    ("Years of Experience", "years_experience", True),
    ("Highest Degree", "highest_degree", True),
    ("School", "school", True),
    ("Field of Study", "field", False),
    ("Graduation Year", "graduation_year", False),
    ("Work Authorization", "work_authorization", False),
    ("Salary Expectation", "salary_expectation", True),
    ("Notice Period", "notice_period", False),
    ("Earliest Start", "earliest_start", False),
    ("Referral / How did you hear", "referral_source", False),
]


@dataclass
class SheetEntry:
    label: str
    canonical_key: str
    required: bool
    value: object = None
    value_source: str = ""
    confidence: str = "missing"  # exact | derived | policy | missing
    note: str = ""
    extra: dict = field(default_factory=dict)


def _confidence_for(value, source: str) -> str:
    if value is None or value in ("", "…"):
        return "missing"
    if "policy" in source:
        return "policy"
    if source.startswith("derived"):
        return "derived"
    return "exact"


def build_fill_sheet(resume: MasterResume) -> list[SheetEntry]:
    values = {e.canonical_key: e for e in field_map(resume)}
    entries: list[SheetEntry] = []
    for label, key, required in CHECKLIST:
        fm = values.get(key)
        value = fm.value if fm else None
        source = fm.source if fm else ""
        entries.append(SheetEntry(
            label=label,
            canonical_key=key,
            required=required,
            value=value,
            value_source=source,
            confidence=_confidence_for(value, source),
            note=fm.note if fm else "",
        ))
    return entries


def sheet_missing(entries: list[SheetEntry]) -> list[SheetEntry]:
    return [e for e in entries if e.required and e.confidence == "missing"]


def sheet_to_yaml_dict(entries: list[SheetEntry]) -> dict:
    return {
        "fields": [
            {
                "label": e.label,
                "canonical_key": e.canonical_key,
                "required": e.required,
                "value": e.value,
                "value_source": e.value_source,
                "confidence": e.confidence,
                "note": e.note,
            }
            for e in entries
        ],
        "unmatched": [],
        "policy_flags": [e.canonical_key for e in entries if e.confidence == "policy"],
    }
