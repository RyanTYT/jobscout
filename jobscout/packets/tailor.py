"""Selection-plan tailoring (PLAN §5.5). Quality-tier LLM produces a PLAN,
not prose: which bullets to include, their order, optional connective-only
rephrasing. The plan is validated against the master resume's real IDs —
unknown references are stripped and flagged, never trusted.
"""

from __future__ import annotations

import json

from jobscout.clients.llm import LlmError, LlmResponse
from jobscout.core.schema import MasterResume

SYSTEM = (
    "You tailor resumes strictly by SELECTION. You are given master-resume "
    "fragments with stable IDs and a job posting. Return ONLY JSON: a "
    "selection plan. You may pick, order, and lightly rephrase connective "
    "language; you may NOT invent facts, numbers, employers, dates, or "
    "degrees. Rephrasing must keep every metric and entity verbatim."
)

_PLAN_KEYS = {"summary", "experience_order", "bullets", "rephrased",
              "projects", "skills_emphasis", "skills_drop", "keyword_alignment"}


def _resume_context(resume: MasterResume) -> str:
    lines = [f"identity: name={resume.identity.full_name!r} "
             f"email={resume.identity.email!r}"]
    for edu in resume.education:
        lines.append(f"education {edu.id}: {edu.degree} {edu.field} @ {edu.school} "
                     f"({edu.dates.start}-{edu.dates.end if edu.dates else '?'})")
    for exp in resume.experience:
        lines.append(f"experience {exp.id}: {exp.title} @ {exp.company} "
                     f"({exp.dates.start}-{exp.dates.end or 'present'})")
        for b in exp.bullets:
            lines.append(f"  bullet {b.id}: {b.text} | metrics={b.metrics}")
    for proj in resume.projects:
        lines.append(f"project {proj.id}: {proj.name} ({proj.role})")
        for b in proj.bullets:
            lines.append(f"  bullet {b.id}: {b.text} | metrics={b.metrics}")
    skills = "; ".join(f"{a.area}: {', '.join(a.items)}" for a in resume.skills.core)
    lines.append(f"skills: {skills}")
    return "\n".join(lines)


def _posting_context(posting_row) -> str:
    if posting_row is None:
        return "(no posting)"
    company = posting_row["company_name"] or posting_row["company_id"] or "?"
    return (f"company: {company}\ntitle: {posting_row['title']}\n"
            f"location: {posting_row['location']}\n"
            f"description: {(posting_row['description'] or '')[:2500]}")


def _prompt(resume: MasterResume, posting_row) -> str:
    return f"""MASTER RESUME (source of truth, stable IDs):
{_resume_context(resume)}

JOB POSTING:
{_posting_context(posting_row)}

Produce a selection plan as JSON exactly like:
{{
  "summary": "2-3 lines for the top of the resume",
  "experience_order": ["<exp id>", "..."],
  "bullets": {{"<exp id>": ["<bullet id>", "..."]}},
  "rephrased": [
    {{"source_id": "<bullet id>", "text": "same facts, tightened connective language", "change": "what changed and why"}}
  ],
  "projects": {{"<project id>": ["<bullet id>", "..."]}},
  "skills_emphasis": ["..."],
  "skills_drop": ["..."],
  "keyword_alignment": {{"matched": ["..."], "gaps": ["..."]}}
}}

Rules: reference ONLY the IDs above; if the resume has no experience or
projects, use empty lists; keyword gaps must NOT be papered over — list them
honestly."""


def parse_plan(text: str, resume: MasterResume) -> tuple[dict, list[str]]:
    """Parse + validate the plan. Returns (plan, issues). Unknown-ID
    references are dropped and reported as issues — never trusted."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(t)
    except ValueError:
        return {}, ["plan is not valid JSON"]

    issues: list[str] = []
    data = {k: data.get(k) for k in _PLAN_KEYS}
    data.setdefault("summary", "")
    for listkey in ("experience_order", "skills_emphasis", "skills_drop"):
        if not isinstance(data.get(listkey), list):
            data[listkey] = []

    exp_ids = {e.id: e for e in resume.experience}
    proj_ids = {p.id: p for p in resume.projects}
    all_bullet_ids = {b.id for e in resume.experience for b in e.bullets} | \
                     {b.id for p in resume.projects for b in p.bullets}

    kept_order: list[str] = []
    for i in data["experience_order"]:
        if isinstance(i, str) and i in exp_ids:
            kept_order.append(i)
        else:
            issues.append(f"unknown exp id {i!r}")
    data["experience_order"] = kept_order
    bullets: dict = data.get("bullets") if isinstance(data.get("bullets"), dict) else {}
    clean_bullets: dict = {}
    for exp_id, ids in bullets.items():
        if exp_id not in exp_ids:
            issues.append(f"unknown exp id in bullets: {exp_id!r}")
            continue
        clean = [b for b in ids if isinstance(b, str) and b in all_bullet_ids]
        dropped = len(ids) - len(clean)
        if dropped:
            issues.append(f"{exp_id}: {dropped} unknown bullet id(s) dropped")
        clean_bullets[exp_id] = clean
    data["bullets"] = clean_bullets

    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    clean_projects: dict = {}
    for proj_id, ids in projects.items():
        if proj_id not in proj_ids:
            issues.append(f"unknown project id: {proj_id!r}")
            continue
        clean = [b for b in ids if isinstance(b, str) and b in all_bullet_ids]
        clean_projects[proj_id] = clean
    data["projects"] = clean_projects

    rephrased = data.get("rephrased") if isinstance(data.get("rephrased"), list) else []
    clean_rephrased = []
    for item in rephrased:
        if not isinstance(item, dict):
            continue
        sid = item.get("source_id")
        if sid not in all_bullet_ids:
            issues.append(f"rephrased references unknown bullet id: {sid!r}")
            continue
        clean_rephrased.append({
            "source_id": sid,
            "text": str(item.get("text") or ""),
            "change": str(item.get("change") or ""),
        })
    data["rephrased"] = clean_rephrased

    ka = data.get("keyword_alignment") if isinstance(data.get("keyword_alignment"), dict) else {}
    data["keyword_alignment"] = {
        "matched": [str(x) for x in ka.get("matched", []) if x],
        "gaps": [str(x) for x in ka.get("gaps", []) if x],
    }
    return data, issues


def tailor_posting(llm, posting_row, resume: MasterResume) -> tuple[dict, list[str], float]:
    """Returns (plan, issues, cost_usd). Plan is {} when tailoring failed."""
    try:
        resp = llm.chat(
            "quality",
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": _prompt(resume, posting_row)}],
            json_mode=True,
        )
    except LlmError:
        return {}, ["LLM call failed"], 0.0
    plan, issues = parse_plan(resp.text, resume)
    return plan, issues, resp.cost_usd


class FakeTailorModel:
    """Canned model for --dry-run: a plan that is valid for ANY resume."""

    def chat(self, tier, messages, *, json_mode=True, cache_key=None, tools=None):
        plan = {
            "summary": "Dry-run tailored summary: senior engineer, "
                       "details pending the filled master resume.",
            "experience_order": [],
            "bullets": {},
            "rephrased": [],
            "projects": {},
            "skills_emphasis": [],
            "skills_drop": [],
            "keyword_alignment": {"matched": [], "gaps": []},
        }
        return LlmResponse(
            text=json.dumps(plan), model="fake-tailor",
            prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
            tool_calls=None,
        )
