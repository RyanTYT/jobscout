"""Packet orchestrator (PLAN §5.8, §10 P6): one selected posting → complete
packet in applications/{company-slug}-{date}/, claim-checked, with a manifest
and DB state. Status: needs_input (with reasons) or ready — `ready` requires
the claim-check gate to pass AND no missing required fields AND real (or
dry-run) tailored content to exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from jobscout.core import db
from jobscout.core import paths as core_paths
from jobscout.core.resume import ResumeError, load_master_resume
from jobscout.packets import claim_check as cc
from jobscout.packets import field_map as fm
from jobscout.packets import render
from jobscout.packets.cover_letter import FakeCoverModel, cover_letter
from jobscout.packets.tailor import FakeTailorModel, tailor_posting


class PacketError(Exception):
    pass


def prepare_packet(conn, posting_id: str, *, dry_run: bool = False,
                   force: bool = False) -> dict:
    posting = db.get_posting(conn, posting_id)
    if posting is None:
        raise PacketError(f"no such posting: {posting_id}")
    try:
        resume = load_master_resume()
    except ResumeError as e:
        raise PacketError(str(e)) from e

    packet_id = f"pk-{db.sha256(posting_id)[:12]}"
    company = posting["company_name"] or posting["company_id"] or "company"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    slug = db.slugify(company)
    out_dir = core_paths.applications_dir() / f"{slug}-{today}"
    out_dir.mkdir(parents=True, exist_ok=True)

    reasons: list[str] = []
    cost = 0.0
    model = None

    # 1. fill sheet (deterministic)
    sheet = fm.build_fill_sheet(resume)
    missing = fm.sheet_missing(sheet)
    if missing:
        reasons.append(
            f"master resume missing {len(missing)} required field(s): "
            + ", ".join(e.label for e in missing)
        )
    (out_dir / "fill_sheet.yaml").write_text(
        yaml.safe_dump(fm.sheet_to_yaml_dict(sheet), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 2. tailor (quality tier; fake model for dry-run; skipped without key)
    llm = None
    if dry_run:
        llm = FakeTailorModel()
        model = "fake-tailor"
    else:
        from jobscout.llm import LlmClient

        client = LlmClient(conn=conn)
        if client.available:
            llm = client
            model = client.tier_cfg("quality").model
    plan: dict = {}
    tailor_issues: list[str] = []
    if llm is not None:
        plan, tailor_issues, c = tailor_posting(llm, posting, resume)
        cost += c
        if not plan:
            reasons.append("tailoring failed — see tailor.yaml")
    else:
        reasons.append("LLM key required for tailoring and cover letter "
                       "(set JOBSCOUT_LLM_API_KEY in .env, or use --dry-run)")
    (out_dir / "tailor.yaml").write_text(
        yaml.safe_dump(plan or {}, sort_keys=False, allow_unicode=True) +
        ("".join(f"# issue: {i}\n" for i in tailor_issues)),
        encoding="utf-8",
    )

    # 3. cover letter
    letter = ""
    if dry_run:
        letter, c = cover_letter(FakeCoverModel(), posting, resume)
        cost += c
    elif llm is not None:
        letter, c = cover_letter(llm, posting, resume)
        cost += c
        if not letter:
            reasons.append("cover letter generation failed")
    if letter:
        (out_dir / "cover_letter.md").write_text(letter, encoding="utf-8")

    # 4. claim check (deterministic, always runs on generated text)
    claim_rows = cc.check_packet(plan, letter, resume, posting)
    (out_dir / "claim_check.yaml").write_text(
        yaml.safe_dump(claim_rows, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    unsupported = cc.unsupported_count(claim_rows)
    if unsupported:
        reasons.append(
            f"claim-check gate: {unsupported} unsupported claim(s) — resolve "
            "before ready (see claim_check.yaml)"
        )

    # 5. render (md always; pdf when typst exists)
    (out_dir / "resume.md").write_text(render.render_resume_md(plan or None, resume),
                                       encoding="utf-8")
    pdf = render.render_typst_pdf(out_dir, plan or None, resume)
    if pdf is None:
        reasons.append("typst not installed — resume.md only "
                       "(brew install typst for PDF)")

    # 6. status + manifest
    status = "needs_input" if reasons else "ready"
    manifest = {
        "packet_id": packet_id,
        "posting_id": posting_id,
        "posting_title": posting["title"],
        "company": company,
        "posting_url": posting["url"],
        "status": status,
        "reasons": reasons,
        "model": model,
        "cost_usd": round(cost, 6),
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "dry_run": dry_run,
        "files": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
    }
    (out_dir / "packet.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 7. DB state
    db.upsert_packet(conn, packet_id=packet_id, posting_id=posting_id,
                     status=status, dir_path=str(out_dir), model=model,
                     cost_usd=cost)
    db.set_posting_status(conn, posting_id, f"packet:{status}")
    return {"packet_id": packet_id, "status": status, "reasons": reasons,
            "dir": str(out_dir), "cost": cost, "unsupported": unsupported,
            "missing": [e.label for e in missing]}


def read_packet_files(dir_path: str) -> dict[str, str]:
    d = Path(dir_path)
    out: dict[str, str] = {}
    for name in ("packet.yaml", "fill_sheet.yaml", "claim_check.yaml",
                 "tailor.yaml", "resume.md", "cover_letter.md"):
        p = d / name
        if p.is_file():
            out[name] = p.read_text(encoding="utf-8")
    return out
