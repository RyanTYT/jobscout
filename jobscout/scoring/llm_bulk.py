"""Tier-1 (bulk) LLM scoring — PLAN §2.

final = fit×0.6 + company_quality×0.25 + opportunity×0.15
  * fit — from the LLM (role/stack/seniority/location fit vs distilled profile)
  * company_quality — tier prior (A=90, B=70, C=50, candidate=30)
  * opportunity — the dark-pool thesis: ATS-weakness + competition adjustment
    (no board = 85, one board = 60, many boards = 35; ±10 by perceived competition)

Cached by (profile_version, content_hash): a posting is scored once, ever —
unless the profile changes, which re-scores only unscored/active postings.
Caps: stops on tier 'bulk' daily cap, degrades to rule-only.
"""

from __future__ import annotations

import json

from jobscout.clients.llm import CapExceeded, LlmClient, LlmError
from jobscout.core import db
from jobscout.core.schema import ProfileCfg

TIER_SCORES = {"A": 90, "B": 70, "C": 50, "candidate": 30}

_SYSTEM = (
    "You are a precise job-posting screener for a senior engineer. "
    "Respond with strict JSON only — no prose, no markdown fences."
)


def _user_prompt(profile: ProfileCfg, row) -> str:
    t = profile.target
    return f"""TARGET CANDIDATE PROFILE (distilled):
roles: {", ".join(t.roles)} (weighting: {json.dumps(t.weighting)})
seniorities: {", ".join(t.seniorities)}
stack: {", ".join(t.stack)}
domains: {", ".join(t.domains)}
locations: {", ".join(t.locations) or "any"}
remote: allowed={t.remote.allowed}, preference={t.remote.preference}
dealbreakers: {", ".join(profile.dealbreakers) or "none"}

JOB POSTING:
company: {row["company_id"]} (tier {row["tier"] or "?"})
title: {row["title"]}
location: {row["location"] or "unknown"}
level guess: {row["seniority"] or "unknown"}
source: {row["source"]}
description: {(row["description"] or "")[:2500] or "(none)"}

Score this posting for the candidate. Respond with JSON exactly like:
{{"fit": <0-100 int, role/stack/seniority/location fit>,
  "stack": [<relevant technologies>],
  "seniority": "<your read of the level>",
  "flags": [<short flags, e.g. "crypto", "on-call">],
  "rationale": "<at most 2 sentences>",
  "red_flags": [<concerns a candidate would want to know, or empty>],
  "competition": "<low|medium|high — broadly advertised (boards/aggregators)=high, niche company page=low>"}}"""


def cache_key(profile: ProfileCfg, row) -> str:
    content = row["content_hash"] or db.sha256(f"{row['title']}|{row['description'] or ''}")
    return db.sha256(f"bulk-v1|{profile.profile_version}|{content}")


def _parse(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        data = json.loads(t)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fit = data.get("fit")
    if not isinstance(fit, (int, float)):
        return None
    data["fit"] = max(0, min(100, int(fit)))
    comp = str(data.get("competition") or "medium").lower()
    data["competition"] = comp if comp in ("low", "medium", "high") else "medium"
    for key in ("stack", "flags", "red_flags"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data["rationale"] = str(data.get("rationale") or "")[:500]
    data["seniority"] = str(data.get("seniority") or "")[:40]
    return data


def score_posting(row, profile: ProfileCfg, llm: LlmClient, conn) -> dict | None:
    """Score one posting. Cache hit → no LLM call. Returns parsed dict or None."""
    key = cache_key(profile, row)
    cached = conn.execute(
        "SELECT response FROM llm_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if cached:
        try:
            return json.loads(cached["response"])
        except ValueError:
            pass  # corrupted cache entry — fall through and re-score

    resp = llm.chat("bulk", [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _user_prompt(profile, row)},
    ], cache_key=key)
    parsed = _parse(resp.text)
    if parsed is None:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (cache_key, response, model) VALUES (?, ?, ?)",
        (key, json.dumps(parsed), resp.model),
    )
    conn.commit()
    return parsed


def compute_final(parsed: dict, tier: str | None, non_ats: int | None,
                  boards_count: int) -> dict:
    tier_score = TIER_SCORES.get(tier or "", 50)
    if non_ats:
        opportunity = 85.0
    else:
        opportunity = 60.0 if boards_count <= 1 else 35.0
    comp = parsed.get("competition", "medium")
    if comp == "low":
        opportunity = min(100.0, opportunity + 10)
    elif comp == "high":
        opportunity = max(0.0, opportunity - 10)
    fit = float(parsed["fit"])
    final = round(0.6 * fit + 0.25 * tier_score + 0.15 * opportunity, 1)
    return {"fit": fit, "company_quality": tier_score, "opportunity": opportunity, "final": final}


def _boards_count(row) -> int:
    raw = row["ats_tokens"] if "ats_tokens" in row.keys() else None
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except ValueError:
        return 0


def score_postings(conn, rows, profile: ProfileCfg, llm: LlmClient) -> dict:
    """Score rows (already tier-prioritised by the caller). Returns
    {"scored": n, "cached": n, "cost": usd, "capped": bool, "errors": n}."""
    stats = {"scored": 0, "cached": 0, "cost": 0.0, "capped": False, "errors": 0}
    for row in rows:
        key = cache_key(profile, row)
        already = conn.execute(
            "SELECT 1 FROM llm_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        try:
            parsed = score_posting(row, profile, llm, conn)
        except CapExceeded:
            stats["capped"] = True
            break
        except LlmError:
            stats["errors"] += 1
            if stats["errors"] >= 5:
                break  # provider is unhappy — stop early
            continue
        if parsed is None:
            stats["errors"] += 1
            continue
        scores = compute_final(parsed, row["tier"], row["non_ats"], _boards_count(row))
        db.update_posting_scores(
            conn, row["id"],
            fit=scores["fit"], company_quality=scores["company_quality"],
            opportunity=scores["opportunity"], final=scores["final"],
            llm_json=json.dumps(parsed), cache_key=key,
        )
        stats["scored"] += 1
        if already:
            stats["cached"] += 1
        else:
            stats["cost"] = round(stats["cost"] + 0.0, 6)  # cost tracked via llm_calls
    return stats
