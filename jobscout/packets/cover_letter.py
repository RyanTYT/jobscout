"""Cover letter generation (PLAN §5.6): hook / fit / proof / close, grounded
in research notes + master resume. The claim-check gate screens the output.
"""

from __future__ import annotations

from jobscout.core.paths import research_dir
from jobscout.core.schema import MasterResume
from jobscout.llm import LlmError, LlmResponse

SYSTEM = (
    "You write concise, honest cover letters (250-350 words, first person). "
    "Structure: hook (why THIS company), fit (which of the candidate's "
    "experience applies), proof (one specific, true story), close "
    "(logistics). You may ONLY state facts that appear in the provided "
    "master resume or research notes. If evidence is thin, be brief — "
    "never pad with invented achievements."
)


def _research_note(company_name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (company_name or "").lower())
    candidates = sorted(research_dir().glob(f"*{slug[:20]}*.md"))
    if not candidates:
        return "(no research notes — keep the hook brief and general)"
    return candidates[-1].read_text(encoding="utf-8")[:1500]


def _prompt(resume: MasterResume, posting_row, research: str) -> str:
    company = posting_row["company_name"] or posting_row["company_id"] or "the company"
    identity = resume.identity
    return f"""CANDIDATE (master resume, abridged):
name: {identity.full_name}
current: {posting_row and 'see posting context'}
education: {"; ".join(f"{e.degree} {e.field} @ {e.school}" for e in resume.education) or "(none listed)"}
experience: {"; ".join(f"{e.title} @ {e.company}" for e in resume.experience) or "(none listed)"}
skills: {"; ".join(f"{a.area}: {', '.join(a.items)}" for a in resume.skills.core) or "(none listed)"}

JOB POSTING:
company: {company}
title: {posting_row['title']}
description: {(posting_row['description'] or '')[:2000]}

RESEARCH NOTES:
{research}

Write the cover letter now (plain text, no markdown headers)."""


def cover_letter(llm, posting_row, resume: MasterResume) -> tuple[str, float]:
    """Returns (letter, cost). Letter is "" when generation failed."""
    research = _research_note(
        posting_row["company_name"] or posting_row["company_id"] or ""
    )
    try:
        resp = llm.chat(
            "quality",
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": _prompt(resume, posting_row, research)}],
        )
    except LlmError:
        return "", 0.0
    text = (resp.text or "").strip().strip("`")
    if text.lower().startswith("text\n"):
        text = text[5:]
    return text, resp.cost_usd


class FakeCoverModel:
    """Canned letter for --dry-run — deliberately fact-free."""

    def chat(self, tier, messages, *, json_mode=True, cache_key=None, tools=None):
        letter = (
            "Dear Hiring Team,\n\n"
            "I am applying for this role after following your work for some "
            "time. My background is summarized in the attached resume; the "
            "specific experience most relevant to your team is detailed "
            "there. I would welcome a conversation about how it maps to "
            "your needs.\n\n"
            "Sincerely,\n"
            "The Candidate"
        )
        return LlmResponse(
            text=letter, model="fake-cover",
            prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
            tool_calls=None,
        )
