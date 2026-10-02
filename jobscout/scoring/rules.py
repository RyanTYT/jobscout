"""Recall-first rule filter from profile.yaml (PLAN §10 P1).

Deterministic, free, runs BEFORE any LLM call in P2 to cut volume ~10x.
Philosophy: recall-first — only reject when the posting is CLEARLY out of
scope (dealbreaker text, wrong seniority, wrong location, no role overlap).
Unknown/ambiguous values pass through for the (P2) LLM to judge.
"""

from __future__ import annotations

import re

from jobscout.core.schema import ProfileCfg

_DEALBREAKER_PATTERNS = {
    "clearance_required": (
        r"security clearance|top secret|ts/?sci|u\.?s\. citizens? only|"
        r"citizens? (?:is )?required|ability to obtain.{0,20}clearance"
    ),
    "on_call_heavy": r"24/7 on.?call|heavy on.?call|overnight on.?call",
}

_ROLE_SYNONYMS: dict[str, list[str]] = {
    "quant": [
        r"\bquant(?:itative)?\b",
        r"\bpricing\b",
        r"\bderivatives?\b",
        r"\bmarket\s?microstructure\b",
    ],
    "trading": [
        r"\btrading\b",
        r"\bexecution\b",
        r"\bmarket\s?data\b",
        r"\blow[\s-]?laten",
        r"\bmatching engine\b",
        r"\bexchange\b",
        r"\bmarket.?mak",
    ],
    "backend": [
        r"\bback[\s-]?end\b",
        r"\bsoftware engineer(?:ing)?\b",
        r"\bsoftware developer\b",
        r"\bsystems? engineer(?:ing)?\b",
        r"\bplatform engineer(?:ing)?\b",
        r"\binfrastructure engineer(?:ing)?\b",
    ],
}

_TECH_MARKER = re.compile(
    r"engineer|developer|scientist|research(?:er)?|quant|sre|architect|programmer",
    re.I,
)

_SENIORITY_PATTERNS: list[tuple[str, str]] = [
    (r"\bintern(?:ship)?\b", "intern"),
    (r"\bjunior\b|\bgraduate\b", "junior"),
    (r"\bsenior\b|\bsnr\b|\bsr\.?\b", "senior"),
    (r"\bstaff\b", "staff"),
    (r"\bprincipal\b", "principal"),
    (r"\b(?:tech(?:nical)? )?lead\b", "lead"),
    (r"\b(?:engineering )?manager\b|\bhead of\b", "manager"),
    (r"\bdirector\b", "director"),
    (r"\b(?:vp|vice president)\b", "vp"),
]


def guess_seniority(title: str) -> str | None:
    t = (title or "").lower()
    for pat, level in _SENIORITY_PATTERNS:
        if re.search(pat, t):
            return level
    return None


def _flex(role: str) -> str:
    words = [re.escape(w) for w in role.lower().split() if w]
    if not words:
        return ""
    return r"[\s\-_/]{0,3}".join(words)


def _role_matchers(roles: list[str]) -> list[re.Pattern[str]]:
    pats: list[str] = []
    for role in roles:
        f = _flex(role)
        if f:
            pats.append(f)
        key = role.lower()
        for syn_key, syns in _ROLE_SYNONYMS.items():
            if syn_key in key:
                pats.extend(syns)
    out: list[re.Pattern[str]] = []
    seen: set[str] = set()
    for p in pats:
        if p not in seen:
            seen.add(p)
            out.append(re.compile(p, re.I))
    return out


def rule_filter(p, profile: ProfileCfg) -> tuple[bool, list[str]]:
    """p: anything with .title/.description/.location/.remote/.seniority
    (RawPosting or SimpleNamespace built from a postings row)."""
    title = (p.title or "").strip()
    desc = (getattr(p, "description", None) or "")[:2000]
    text = f"{title}\n{desc}"

    for db_name in profile.dealbreakers:
        pat = _DEALBREAKER_PATTERNS.get(db_name, db_name)
        try:
            if re.search(pat, text, re.I):
                return False, [f"dealbreaker:{db_name}"]
        except re.error:
            continue  # unknown symbolic name that isn't a valid regex — skip

    if profile.target.roles:
        matchers = _role_matchers(profile.target.roles)
        if matchers:
            # Tech-marker gate: an engineering-ish title is required, otherwise
            # keyword noise like "Account Executive ... (Trading)" sails through.
            # Recall-first: the tech marker alone keeps a posting — role overlap
            # is the (P2) LLM's call, not this gate's. Restored 2026-10-02 from
            # bytecode; role_title/role_desc were already dead in the erased
            # version (the edit was mid-flight). They cost two regex scans per
            # posting, so delete them once the intent is settled.
            tech_title = _TECH_MARKER.search(title) is not None
            role_title = any(m.search(title) for m in matchers)          # noqa: F841
            role_desc = bool(desc) and any(m.search(desc) for m in matchers)  # noqa: F841,E501
            if not tech_title:
                return False, ["role-no-match"]

    seniority = p.seniority or guess_seniority(title)
    wanted = [s.lower() for s in profile.target.seniorities]
    if seniority and wanted and seniority not in wanted:
        return False, [f"seniority:{seniority}"]

    if profile.target.locations:
        loc = (p.location or "").strip()
        loc_l = loc.lower()
        is_remote = bool(p.remote) or ("remote" in loc_l)
        if loc and loc_l not in ("unknown",) and not is_remote:
            if not any(loc_pat.lower() in loc_l for loc_pat in profile.target.locations):
                return False, [f"location:{loc}"]

    if not profile.target.remote.allowed:
        if bool(p.remote) or "remote" in (p.location or "").lower():
            return False, ["remote-not-allowed"]

    return True, ["pass"]
