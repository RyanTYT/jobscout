"""core/resume_parser.py — LLM-based resume parsing and tailoring.

Master resume lives as markdown (config/master_resume.md). The LLM
parses it into structured YAML for the profile store, and tailors a
version for each job description.
"""

from __future__ import annotations

from pathlib import Path

from jobscout.clients.llm import LlmClient, LlmError
from jobscout.core import paths as core_paths

_PARSE_SYSTEM = (
    "You are a precise resume parser. Extract structured YAML from the "
    "candidate's free-form markdown resume. Output ONLY valid YAML — no "
    "prose, no markdown fences."
)

_PARSE_TEMPLATE = """\
Parse this master resume into structured YAML with these exact sections:

summary: <string>
experience:
  - company: <string>
    role: <string>
    start: <string YYYY-MM>
    end: <string YYYY-MM or "present">
    location: <string>
    bullets:
      - <string>
projects:
  - name: <string>
    description: <string>
    technologies: [<string>]
    link: <string>
    bullets:
      - <string>
skills:
  languages: [<string>]
  frameworks: [<string>]
  tools: [<string>]
  domains: [<string>]
education:
  - degree: <string>
    school: <string>
    year: <string>
    details: <string>
certifications: [<string>]
publications: [<string>]
languages: [<string>]

Rules:
- Preserve every skill, project, and experience from the original.
- Dates use YYYY-MM format; "present" for current roles.
- Bullets rewrite for clarity but keep the substance.
- If a section is missing, use an empty list or null.

MASTER RESUME:
{resume_text}

YAML:"""

_TAILOR_SYSTEM = (
    "You are a expert resume writer and career coach. Given a master "
    "resume and a job description, produce a tailored resume markdown "
    "(1 page max) that highlights the most relevant experience, projects, "
    "and skills. Never invent facts — reorder, rephrase, and emphasize. "
    "Output only valid markdown."
)

_TAILOR_TEMPLATE = """\
Master Resume (YAML):
{resume_yaml}

Job Description:
{job_description}

Tailored Resume (markdown):"""


def master_resume_path() -> Path:
    return core_paths.config_dir() / "master_resume" / "resume.md"


def load_master_resume() -> str:
    p = master_resume_path()
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def save_master_resume(text: str) -> Path:
    p = master_resume_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def parse_resume(text: str | None = None, llm: LlmClient | None = None) -> dict:
    """Parse the master resume markdown into structured YAML dict.
    Returns dict with 'parsed' (the structured data) or 'error' on failure."""
    from jobscout.core.config import load_profile

    resume_text = text or load_master_resume()
    if not resume_text.strip():
        return {"error": "resume.md is empty. Write your resume in markdown first, then parse."}

    if llm is None:
        from jobscout.core import db as _db

        _db.init_db()
        conn = _db.connect()
        try:
            llm = LlmClient(conn=conn)
        finally:
            conn.close()

    if not llm.available:
        return {"error": "LLM not available — set JOBSCOUT_LLM_API_KEY in .env"}

    try:
        resp = llm.chat("bulk", [
            {"role": "system", "content": _PARSE_SYSTEM},
            {"role": "user", "content": _PARSE_TEMPLATE.format(
                resume_text=resume_text
            )},
        ], json_mode=False)  # YAML output, not JSON
    except LlmError as e:
        return {"error": f"LLM call failed: {e}"}

    import yaml as _yaml

    try:
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("yaml"):
                text = text[4:]
            text = text.strip()
        parsed = _yaml.safe_load(text)
    except Exception as e:
        return {"error": f"Failed to parse LLM response as YAML: {e}", "raw": resp.text}

    if not isinstance(parsed, dict):
        return {"error": "LLM did not return a dict", "raw": resp.text}

    # Validate against MasterResume schema and add stable IDs where missing
    from jobscout.core.schema import MasterResume
    from jobscout.core.db import sha256, slugify

    _next_id = 0
    def _gen_id(prefix: str) -> str:
        nonlocal _next_id
        _next_id += 1
        return f"{prefix}{_next_id}"

    # Ensure required nested structures exist
    parsed.setdefault("version", 3)
    parsed.setdefault("identity", {})
    parsed.setdefault("work_authorization", [])
    parsed.setdefault("education", [])
    parsed.setdefault("experience", [])
    parsed.setdefault("projects", [])
    parsed.setdefault("skills", {"core": [], "certifications": [], "publications": []})
    parsed.setdefault("extras", {})

    # Add stable IDs to experience entries
    for i, exp in enumerate(parsed["experience"]):
        if not exp.get("id"):
            exp["id"] = _gen_id("EXP")
        if not exp.get("dates") and (exp.get("start") or exp.get("end")):
            exp["dates"] = {"start": exp.pop("start", ""), "end": exp.pop("end", None)}
        elif not exp.get("dates"):
            exp["dates"] = {"start": ""}
        if "bullets" in exp and exp["bullets"]:
            for j, b in enumerate(exp["bullets"]):
                if isinstance(b, str):
                    exp["bullets"][j] = {"id": _gen_id("B"), "text": b}
                elif isinstance(b, dict) and not b.get("id"):
                    b["id"] = _gen_id("B")
        if "tech" not in exp:
            exp["tech"] = []

    # Add stable IDs to project entries
    for i, proj in enumerate(parsed.get("projects", [])):
        if not proj.get("id"):
            proj["id"] = _gen_id("PRJ")
        if "bullets" in proj and proj["bullets"]:
            for j, b in enumerate(proj["bullets"]):
                if isinstance(b, str):
                    proj["bullets"][j] = {"id": _gen_id("B"), "text": b}
                elif isinstance(b, dict) and not b.get("id"):
                    b["id"] = _gen_id("B")
        if "tech" not in proj:
            proj["tech"] = []

    # Add stable IDs to education entries
    for i, edu in enumerate(parsed.get("education", [])):
        if not edu.get("id"):
            edu["id"] = _gen_id("EDU")
        if not edu.get("dates") and (edu.get("start") or edu.get("year")):
            edu["dates"] = {"start": edu.pop("start", ""), "end": edu.pop("end", None) or edu.pop("year", None)}
        elif not edu.get("dates"):
            edu["dates"] = {"start": ""}

    # Validate against schema
    try:
        validated = MasterResume(**parsed)
        # Serialize back to dict
        validated_dict = validated.model_dump(exclude_none=True)
    except Exception as e:
        return {"error": f"Schema validation failed: {e}", "raw": resp.text, "attempted": parsed}

    return {"parsed": validated_dict, "model": resp.model, "cost": resp.cost_usd}


def tailor_resume(job_description: str, llm: LlmClient) -> str:
    """Generate a tailored resume markdown for a specific job."""
    from jobscout.core.resume import load_master_resume as load_structured

    resume_yaml = load_structured().model_dump()
    import yaml as _yaml

    yaml_str = _yaml.dump(resume_yaml, default_flow_style=False)

    try:
        resp = llm.chat("bulk", [
            {"role": "system", "content": _TAILOR_SYSTEM},
            {"role": "user", "content": _TAILOR_TEMPLATE.format(
                resume_yaml=yaml_str, job_description=job_description
            )},
        ])
    except LlmError as e:
        return f"# Tailoring failed\n\n{e}"

    return resp.text