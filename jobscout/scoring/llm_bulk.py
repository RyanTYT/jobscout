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
from collections.abc import Callable

from jobscout.clients.llm import CapExceeded, LlmClient, LlmError
from jobscout.core import db
from jobscout.core.config import profile_hash
from jobscout.core.schema import ProfileCfg

# (stats snapshot, rows finished, rows in this pass) — fired before each batch
# and once at the end so a multi-hour pass can report position.
ProgressCb = Callable[[dict, int, int], None]

TIER_SCORES = {"A": 90, "B": 70, "C": 50, "candidate": 30}

BATCH_SIZE = 20          # postings per LLM call (settings.discovery.pipeline)
DESC_CHARS = 1200        # per-posting description budget inside a batch prompt

_SYSTEM = (
    "You are a precise job-posting screener for a senior engineer. "
    "Respond with strict JSON only — no prose, no markdown fences."
)

_SYSTEM_BATCH = (
    "You are a precise job-posting screener for a senior engineer. You are "
    "given SEVERAL postings and must score EVERY one of them. Respond with "
    "strict JSON only — no prose, no markdown fences, no commentary."
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
    """The per-posting cache key: (profile content hash, posting content).

    Uses the profile *hash*, not the profile_version counter, so a no-op save
    of the hunting profile does not invalidate every cached score. Written to
    postings.llm_cache_key as scoring proceeds, which is what makes an
    interrupted pass resumable.
    """
    content = row["content_hash"] or db.sha256(f"{row['title']}|{row['description'] or ''}")
    return db.sha256(f"bulk-v2|{profile_hash(profile)}|{content}")


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


# ── batch scoring ─────────────────────────────────────────────────────────────


def _profile_block(profile: ProfileCfg) -> str:
    t = profile.target
    return f"""TARGET CANDIDATE PROFILE (distilled):
roles: {", ".join(t.roles)} (weighting: {json.dumps(t.weighting)})
seniorities: {", ".join(t.seniorities)}
stack: {", ".join(t.stack)}
domains: {", ".join(t.domains)}
locations: {", ".join(t.locations) or "any"}
remote: allowed={t.remote.allowed}, preference={t.remote.preference}
dealbreakers: {", ".join(profile.dealbreakers) or "none"}"""


def _build_batch_prompt(profile: ProfileCfg, rows: list) -> str:
    """One prompt carrying N postings; the reply must carry N results."""
    blocks = []
    for row in rows:
        desc = (row["description"] or "")[:DESC_CHARS] or "(none)"
        blocks.append(
            f"--- posting id: {row['id']}\n"
            f"company: {row['company_id']} (tier {row['tier'] or '?'})\n"
            f"title: {row['title']}\n"
            f"location: {row['location'] or 'unknown'}\n"
            f"level guess: {row['seniority'] or 'unknown'}\n"
            f"source: {row['source']}\n"
            f"description: {desc}"
        )
    return f"""{_profile_block(profile)}

JOB POSTINGS ({len(rows)} of them, each with its own id):
{chr(10).join(blocks)}

Score EVERY posting above. Respond with JSON exactly in this shape:
{{"results": [{{"id": "<the posting id, copied exactly>",
   "fit": <0-100 int, role/stack/seniority/location fit>,
   "stack": [<relevant technologies>],
   "seniority": "<your read of the level>",
   "flags": [<short flags, e.g. "crypto", "on-call">],
   "rationale": "<at most 2 sentences>",
   "red_flags": [<concerns a candidate would want to know, or empty>],
   "competition": "<low|medium|high — broadly advertised (boards/aggregators)=high, niche company page=low>"}}]}}

Rules: exactly {len(rows)} entries, one per posting, each "id" copied exactly as
given. Do not merge, skip, reorder, or invent postings."""


def _parse_batch(text: str, expect_ids: list[str]) -> dict[str, dict] | None:
    """Parse a batch reply into {posting_id: parsed_result}.

    Accepts {"results": [...]}, a bare top-level list, or a fenced block.

    Matching, in order:
      * full length, no usable ids → positional (one result per posting, in
        order). Positional is only trusted at equal length, where alignment
        cannot be wrong.
      * otherwise → by id, keeping only the expected ids. A short reply is
        salvaged: the caller falls back per-posting for whatever is missing.
      * nothing usable → None, and the whole chunk goes to the per-posting path.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    if not t:
        return None
    try:
        data = json.loads(t)
    except ValueError:
        return None
    if isinstance(data, dict):
        items = data.get("results")
        if not isinstance(items, list):
            return None
    elif isinstance(data, list):
        items = data
    else:
        return None

    # Ids the model actually supplied. If any are present we match strictly by
    # id and never fall back to position — a mislabelled id must become a
    # fallback call, not a score attached to the wrong posting.
    supplied = {
        i["id"] for i in items
        if isinstance(i, dict) and isinstance(i.get("id"), str)
    }

    if not supplied:
        # no ids at all: trust order, but only at equal length
        parsed_items = [p for p in (_parse_one(i) for i in items) if p is not None]
        if len(parsed_items) == len(expect_ids):
            return dict(zip(expect_ids, parsed_items, strict=True))
        return None

    # by id — salvage whatever came back, let the caller fall back for the rest
    wanted = set(expect_ids)
    by_id: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        parsed = _parse_one(item)
        pid = item.get("id")
        if parsed is None or not isinstance(pid, str) or pid not in wanted:
            continue
        by_id.setdefault(pid, parsed)
    return by_id or None


def _parse_one(item: dict) -> dict | None:
    """Field-level validation for one result, reusing the single-item rules."""
    if not isinstance(item, dict):
        return None
    fit = item.get("fit")
    if not isinstance(fit, (int, float)) or isinstance(fit, bool):
        return None
    out = dict(item)
    out["fit"] = max(0, min(100, int(fit)))
    comp = str(out.get("competition") or "medium").lower()
    out["competition"] = comp if comp in ("low", "medium", "high") else "medium"
    for key in ("stack", "flags", "red_flags"):
        if not isinstance(out.get(key), list):
            out[key] = []
    out["rationale"] = str(out.get("rationale") or "")[:500]
    out["seniority"] = str(out.get("seniority") or "")[:40]
    return out


def _cache_put(conn, key: str, parsed: dict, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (cache_key, response, model) VALUES (?, ?, ?)",
        (key, json.dumps(parsed), model),
    )
    conn.commit()


def _write_score(conn, row, parsed: dict, key: str) -> None:
    """Persist one parsed result: llm_cache entry + the posting's score row.

    Writing the cache entry before the score row means an interrupted pass
    never leaves a posting marked as needing work it has already paid for.
    """
    _cache_put(conn, key, parsed, "batch")
    scores = compute_final(parsed, row["tier"], row["non_ats"], _boards_count(row))
    db.update_posting_scores(
        conn, row["id"],
        fit=scores["fit"], company_quality=scores["company_quality"],
        opportunity=scores["opportunity"], final=scores["final"],
        llm_json=json.dumps(parsed), cache_key=key,
    )


def score_postings_batch(
    conn, rows, profile: ProfileCfg, llm: LlmClient,
    batch_size: int | None = None,
    on_progress: ProgressCb | None = None,
) -> dict:
    """Score rows in batches — one LLM call per `batch_size` postings.

    Per-posting cache keys are preserved (never one key per batch), so rows
    already scored against this profile cost no call, and a pass interrupted
    by the spend cap or the row limit resumes where it stopped.

    A batch reply that is unusable, or that does not cover every posting in
    the chunk, falls back to the per-posting path for the rows it missed.

    `on_progress(stats, done, total)` fires before each batch and once at the
    end, so a caller can report position instead of looking stalled.

    Returns {"scored", "cached", "batches", "requests", "fallback", "cost",
             "capped", "cap_reason", "errors"}.
    """
    n = batch_size or BATCH_SIZE
    stats = {"scored": 0, "cached": 0, "batches": 0, "requests": 0,
             "fallback": 0, "cost": 0.0, "capped": False,
             "cap_reason": None, "errors": 0}
    rows = list(rows)
    if not rows:
        return stats

    total = len(rows)
    for start in range(0, len(rows), n):
        _emit(on_progress, stats, start, total)
        chunk = rows[start:start + n]
        keys = {r["id"]: cache_key(profile, r) for r in chunk}
        pending = []
        for row in chunk:
            key = keys[row["id"]]
            hit = conn.execute(
                "SELECT 1 FROM llm_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if hit:
                stats["cached"] += 1
            else:
                pending.append((row, key))

        if not pending:
            continue

        stats["batches"] += 1
        try:
            resp = llm.chat("bulk", [
                {"role": "system", "content": _SYSTEM_BATCH},
                {"role": "user", "content": _build_batch_prompt(
                    profile, [r for r, _ in pending])},
            ])
            stats["requests"] += 1
            stats["cost"] = round(stats["cost"] + resp.cost_usd, 6)
        except CapExceeded as e:
            # Record *why* it stopped. A capped pass and a pass with nothing
            # to do are otherwise indistinguishable in the stats.
            stats["capped"] = True
            stats["cap_reason"] = str(e)
            break
        except LlmError:
            stats["errors"] += 1
            if stats["errors"] >= 5:
                break
            _fallback(conn, stats, pending, profile, llm)
            if stats["capped"] or stats["errors"] >= 5:
                break
            continue

        parsed_map = _parse_batch(resp.text, [r["id"] for r, _ in pending])
        if parsed_map is None:
            stats["errors"] += 1
            _fallback(conn, stats, pending, profile, llm)
            if stats["capped"] or stats["errors"] >= 5:
                break
            continue

        missed = [(r, k) for r, k in pending if r["id"] not in parsed_map]
        for row, key in pending:
            parsed = parsed_map.get(row["id"])
            if parsed is None:
                continue
            _write_score(conn, row, parsed, key)
            stats["scored"] += 1
        if missed:
            _fallback(conn, stats, missed, profile, llm)
            if stats["capped"] or stats["errors"] >= 5:
                break

    _emit(on_progress, stats, total, total)
    return stats


def _emit(cb: ProgressCb | None, stats: dict, done: int, total: int) -> None:
    """Hand a progress snapshot to the caller, if it wants one."""
    if cb is not None:
        cb(stats, done, total)


def _fallback(conn, stats: dict, pending: list, profile: ProfileCfg,
              llm: LlmClient) -> None:
    """Score rows the batch path could not cover, one LLM call each.

    Reuses score_posting, so the cache key is derived exactly as the batch
    path derives it and a row scored here is cached like any other.
    """
    for row, key in pending:
        stats["fallback"] += 1
        try:
            parsed = score_posting(row, profile, llm, conn)
        except CapExceeded as e:
            stats["capped"] = True
            stats["cap_reason"] = str(e)
            return
        except LlmError:
            stats["errors"] += 1
            if stats["errors"] >= 5:
                return
            continue
        if parsed is None:
            stats["errors"] += 1
            continue
        _write_score(conn, row, parsed, key)
        stats["scored"] += 1


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
