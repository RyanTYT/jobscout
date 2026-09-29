"""Render packet artifacts: resume.md always; resume.pdf via typst when the
binary is available (PLAN §5.8)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from jobscout.core.models import MasterResume


def _bullet_text(resume: MasterResume, bullet_id: str, rephrased: dict) -> str:
    override = rephrased.get(bullet_id)
    if override:
        return override
    for exp in resume.experience:
        for b in exp.bullets:
            if b.id == bullet_id:
                return b.text
    for proj in resume.projects:
        for b in proj.bullets:
            if b.id == bullet_id:
                return b.text
    return ""


def _selected_sections(plan: dict | None, resume: MasterResume) -> dict:
    """Apply the selection plan to the resume → renderable structure.
    plan=None (no LLM) → the raw resume as-is."""
    rephrased = {r["source_id"]: r["text"] for r in (plan or {}).get("rephrased", [])}
    plan = plan or {}

    if plan:
        exp_order = [i for i in plan.get("experience_order", []) if i]
        exp_order += [e.id for e in resume.experience if e.id not in exp_order]
        exps = [e for e in resume.experience if e.id in set(exp_order)]
        exps.sort(key=lambda e: exp_order.index(e.id))
        exp_sections = []
        for e in exps:
            ids = plan.get("bullets", {}).get(e.id)
            if ids is None:  # no selection for this exp → include all
                ids = [b.id for b in e.bullets]
            exp_sections.append({
                "company": e.company, "title": e.title or "",
                "dates": f"{e.dates.start} – {e.dates.end or 'present'}",
                "bullets": [_bullet_text(resume, i, rephrased) for i in ids],
            })
        proj_sections = []
        for pid, ids in plan.get("projects", {}).items():
            proj = next((p for p in resume.projects if p.id == pid), None)
            if proj is None:
                continue
            proj_sections.append({
                "name": proj.name,
                "bullets": [_bullet_text(resume, i, rephrased) for i in ids],
            })
    else:
        exp_sections = [{
            "company": e.company, "title": e.title or "",
            "dates": f"{e.dates.start} – {e.dates.end or 'present'}",
            "bullets": [b.text for b in e.bullets],
        } for e in resume.experience]
        proj_sections = [{
            "name": p.name, "bullets": [b.text for b in p.bullets],
        } for p in resume.projects]

    skills = plan.get("skills_emphasis") or None
    return {
        "summary": plan.get("summary", ""),
        "experience": [s for s in exp_sections if s["bullets"] or plan],
        "projects": proj_sections,
        "skills": skills,
    }


def render_resume_md(plan: dict | None, resume: MasterResume) -> str:
    ident = resume.identity
    sections = _selected_sections(plan, resume)
    lines: list[str] = []
    lines.append(f"# {ident.full_name or '(name pending)'}")
    contact = " · ".join(filter(None, [ident.email, ident.phone, ident.links.personal_website,
                                       ident.links.linkedin, ident.links.github]))
    lines.append(contact or "(contact pending — fill master_resume/resume.yaml)")
    lines.append("")
    if sections["summary"]:
        lines.append(sections["summary"])
        lines.append("")
    if sections["experience"]:
        lines.append("## Experience")
        for s in sections["experience"]:
            lines.append(f"**{s['company']}** — {s['title']} ({s['dates']})")
            for b in s["bullets"]:
                lines.append(f"- {b}")
            lines.append("")
    if sections["projects"]:
        lines.append("## Projects")
        for s in sections["projects"]:
            lines.append(f"**{s['name']}**")
            for b in s["bullets"]:
                lines.append(f"- {b}")
            lines.append("")
    skills = sections["skills"]
    if not skills:
        skills = [f"{a.area}: {', '.join(a.items)}" for a in resume.skills.core if a.items]
    if skills:
        lines.append("## Skills")
        lines.append("; ".join(skills))
    if plan is None:
        lines.append("")
        lines.append("> note: rendered from the raw master resume — tailoring "
                     "requires an LLM key (or --dry-run)")
    return "\n".join(lines) + "\n"


def render_typst_pdf(out_dir: Path, plan: dict | None, resume: MasterResume) -> Path | None:
    """Compile resume.pdf via typst; returns the PDF path, or None when the
    typst binary is missing (resume.md is always written)."""
    typst = shutil.which("typst")
    if typst is None:
        return None
    # the one template source: bundled inside the package (works in dev
    # AND in the frozen app — no repo-relative fallback to keep in sync)
    template = Path(__file__).resolve().parent / "templates" / "resume.typ"
    if not template.is_file():
        return None

    ident = resume.identity
    links = [lk for lk in (ident.links.personal_website, ident.links.linkedin,
                            ident.links.github) if lk]
    sections = _selected_sections(plan, resume)
    summary = sections["summary"]
    if isinstance(summary, list):
        summary = " ".join(str(x) for x in summary)
    data = {
        "name": ident.full_name or "",
        "contact": " · ".join(filter(None, [ident.email, ident.phone,
                                            ident.location.city])),
        "links": links,
        "summary": summary,
        "experience": sections["experience"],
        "projects": sections["projects"],
        "skills": [
            {"area": a.area, "items": a.items} for a in resume.skills.core if a.items
        ],
    }
    # typst resolves --input paths (and json() paths) RELATIVE TO THE
    # TEMPLATE FILE — so the template is copied next to the packet data
    # and compiled from there: `json("packet-data.json")` just works.
    local_template = out_dir / "resume.typ"
    local_template.write_text(template.read_text(encoding="utf-8"),
                              encoding="utf-8")
    (out_dir / "packet-data.json").write_text(json.dumps(data, indent=1),
                                              encoding="utf-8")
    try:
        proc = subprocess.run(
            [typst, "compile", str(local_template), str(out_dir / "resume.pdf"),
             "--input", "data=packet-data.json"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    pdf = out_dir / "resume.pdf"
    return pdf if proc.returncode == 0 and pdf.is_file() else None
