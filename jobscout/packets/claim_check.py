"""The claim-check gate (PLAN §5.7) — deterministic fabrication detection.

Extracts factual claims from generated packet text (percentages, money,
durations, years, degrees, proper-noun entities) and verifies each against
the allowed corpus = master resume (candidate facts) + posting text (company
/ role facts). Anything else is `unsupported`, and a packet with unresolved
unsupported claims cannot reach `ready`.
"""

from __future__ import annotations

import json
import re

CLAIM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("percent", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("money", re.compile(r"\$\s?[\d,.]+[KMB]?")),
    ("duration", re.compile(r"\b\d+\s+(?:years?|months?|weeks?)\b", re.I)),
    ("year_range", re.compile(r"\b(19|20)\d{2}\s*[-–—]\s*((19|20)\d{2}|present)\b", re.I)),
    ("degree", re.compile(
        r"\b(?:B\.?S\.?|M\.?S\.?|Ph\.?D\.?|Bachelors?(?:\s+of\s+\w+)?|"
        r"Masters?(?:\s+of\s+\w+)?|Doctorate)\b")),
]

# Multi-word capitalized sequences = candidate entities (companies, products).
# Single capitalized words are too noisy (sentence starts, role words).
_ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z0-9&.\-]+(?:\s+[A-Z][a-zA-Z0-9&.\-]+)+)\b")

_STOP_ENTITIES = {
    "Software Engineer", "Senior Software", "Software Development",
    "Machine Learning", "Data Engineer", "Full Stack", "Full-Stack",
    "Site Reliability", "Work Authorization", "Cover Letter",
    "Thanks", "Thank You", "Dear Hiring", "Hiring Manager", "Hiring Team",
    "The Candidate", "Master Resume",
}


def extract_claims(text: str) -> list[tuple[str, str]]:
    """Returns [(kind, claim)] — deduplicated, order preserved."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pat in CLAIM_PATTERNS:
        for m in pat.finditer(text or ""):
            claim = m.group(0).strip()
            if claim.lower() not in seen:
                seen.add(claim.lower())
                out.append((kind, claim))
    for m in _ENTITY_RE.finditer(text or ""):
        entity = m.group(1).strip()
        # a stop phrase inside a longer entity (e.g. "Dear Hiring Team" contains
        # "Hiring Team") still marks it as boilerplate, not a factual claim
        if any(s in entity for s in _STOP_ENTITIES) or entity.lower() in seen:
            continue
        if len(entity.split()) >= 2:
            seen.add(entity.lower())
            out.append(("entity", entity))
    return out


def _corpus(resume, posting_row) -> str:
    parts = [json.dumps(resume.model_dump(), default=str)]
    if posting_row is not None:
        for col in ("title", "location", "description", "company_id", "company_name", "source"):
            if col in posting_row.keys():
                parts.append(str(posting_row[col] or ""))
    return " ".join(parts).lower()


def check_text(
    text: str, resume, posting_row, *, section: str = "text"
) -> list[dict]:
    corpus = _corpus(resume, posting_row)
    rows: list[dict] = []
    for kind, claim in extract_claims(text):
        status = "verified" if claim.lower() in corpus else "unsupported"
        rows.append({
            "section": section,
            "kind": kind,
            "claim": claim,
            "status": status,
        })
    return rows


def check_packet(
    plan: dict | None, cover_letter: str | None, resume, posting_row
) -> list[dict]:
    """Check every piece of generated text in a packet."""
    rows: list[dict] = []
    if plan:
        rows.extend(check_text(plan.get("summary", ""), resume, posting_row, section="summary"))
        for item in plan.get("rephrased", []):
            rows.extend(check_text(
                item.get("text", ""), resume, posting_row, section=f"rephrased:{item.get('source_id')}"
            ))
    if cover_letter:
        rows.extend(check_text(cover_letter, resume, posting_row, section="cover_letter"))
    return rows


def gate(rows: list[dict]) -> bool:
    """True when the packet MAY go ready — no unresolved unsupported claims."""
    return not any(r["status"] == "unsupported" for r in rows)


def unsupported_count(rows: list[dict]) -> int:
    return sum(1 for r in rows if r["status"] == "unsupported")
