"""ATS board-token probe — the board-token bootstrap (PLAN §6.1).

For a candidate company: guess slug variants (domain root, name variants)
across Greenhouse/Lever/Ashby/SmartRecruiters and keep every board that
responds with live postings. This is also the ATS-absence half of the
dark-pool detector: no boards found + hiring signals = dark-pool lead.
"""

from __future__ import annotations

import re
import time

import httpx

from jobscout.core.db import slugify
from jobscout.sources.ats import FETCHERS
from jobscout.sources.ats.base import SourceError, SourceNotFound, make_client

PROBE_PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters")


def guess_tokens(name: str, domain: str | None) -> list[str]:
    guesses: list[str] = []
    if domain:
        root = domain.split("://")[-1].split("/")[0]
        base = root.split(".")[0].lower()
        if base:
            guesses.append(base)
    n = re.sub(r"[^a-z0-9\s-]", "", (name or "").lower()).strip()
    if n:
        for v in (n.replace(" ", ""), n.replace(" ", "-")):
            if v:
                guesses.append(v)
        words = n.split()
        if len(words) > 1 and words[0] not in ("the",):
            guesses.append(words[0])
    out: list[str] = []
    for g in guesses:
        if g and g not in out:
            out.append(g)
    return out[:5]


def _variants(tokens: list[str]) -> list[str]:
    vs: list[str] = []
    for t in tokens:
        for v in (t, t.capitalize(), t.upper()):
            if v and v not in vs:
                vs.append(v)
    return vs[:6]


def probe_company(
    name: str,
    domain: str | None = None,
    *,
    client: httpx.Client | None = None,
    hints: dict[str, list[str]] | None = None,
) -> dict:
    """Returns {"tokens": {provider: token}, "jobs": {provider: count}}.

    A provider "hit" requires a 200 response with at least one live posting —
    guards against SmartRecruiters' 200-empty responses for wrong ids.
    Thread-safe when a shared client is passed (httpx.Client is thread-safe).
    """
    tokens: dict[str, str] = {}
    jobs: dict[str, int] = {}
    hints = hints or {}
    own = client is None
    if own:
        client = make_client()
    try:
        for provider in PROBE_PROVIDERS:
            cands: list[str] = []
            for h in hints.get(provider, []):
                if h and h not in cands:
                    cands.append(h)
            for v in _variants(guess_tokens(name, domain)):
                if v and v not in cands:
                    cands.append(v)
            for tok in cands[:4]:
                try:
                    postings = FETCHERS[provider](
                        tok, client, company=name, company_slug=slugify(name), content=False
                    )
                except (SourceNotFound, SourceError):
                    time.sleep(0.15)
                    continue
                except Exception:  # noqa: BLE001 — network hiccup: skip provider
                    break
                if postings:
                    tokens[provider] = tok
                    jobs[provider] = len(postings)
                    break
                # 200 with zero postings:
                # GH/Lever/Ashby 404 on wrong tokens, so this board is real but
                # empty — stop guessing this provider. SmartRecruiters can 200
                # empty on WRONG ids, so keep trying the next guess there.
                if provider != "smartrecruiters":
                    break
                time.sleep(0.15)
    finally:
        if own:
            client.close()
    return {"tokens": tokens, "jobs": jobs}
