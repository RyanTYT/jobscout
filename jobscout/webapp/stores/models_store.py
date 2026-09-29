"""webapp/models_store.py — models.yaml editor + provider price refresh.

models.yaml stays the source of truth for LOCAL policy (which model each
tier uses, spend caps, purposes) — but the PRICES now have a refresh
path: OpenAI-compatible providers serve GET {base_url}/models, and
OpenRouter-style deployments include per-token pricing in that response.
Prices are only used by the spend meter, so a stale or missing price
miscounts cost, never blocks a call.

Edits are line-level patches (comment preserving); validation rolls the
file back on failure.
"""

from __future__ import annotations

import re
from pathlib import Path

from jobscout.core import config as core_config
from jobscout.core import paths as core_paths
from jobscout.webapp.stores import config_store as cs


class ModelsStoreError(Exception):
    pass


def _path() -> Path:
    p = Path(core_paths.config_dir()) / "models.yaml"
    if not p.is_file():
        raise ModelsStoreError(f"missing config file: {p}")
    return p


def current():
    return core_config.load_models_cfg()


TIERS = ("bulk", "agent", "quality")
_SCHEDULES = ("daily", "weekdays", "mon-wed-fri", "manual")


def _num(raw, name):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ModelsStoreError(f"{name} must be a number") from None
    if v < 0:
        raise ModelsStoreError(f"{name} must be >= 0")
    return v


def _tier_index(lines: list[str], tier: str) -> int | None:
    # tier keys live at 2-space indent (under tiers:); caps at column 0
    pat = re.compile(rf"^ {{0,2}}{re.escape(tier)}:\s*$")
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def _set_key(lines: list[str], key: str, value: str, indent: str = "    ",
              start: int = 0, end: int | None = None) -> bool:
    """Bounded, comment-preserving key patch (config_store primitive)."""
    return cs.set_key(lines, key, value, indent=indent, start=start,
                      end=end)


def _region(lines: list[str], tier: str) -> tuple[int, int] | None:
    """(start, end) line range of a tier's block (4-space keys)."""
    i = _tier_index(lines, tier)
    if i is None:
        return None
    end = len(lines)
    for j in range(i + 1, len(lines)):
        s = lines[j]
        if re.match(r"^  \w[\w-]*:\s*$", s) or re.match(r"^\w", s):
            end = j
            break
    return (i + 1, end)


def save(*, tiers: dict, caps: dict) -> dict:
    """tiers: {tier: {model, price_in, price_out, max_daily_usd}};
    caps: {monthly_usd, on_cap}. Patched with rollback on invalid input."""
    parsed = {}
    for tier in TIERS:
        data = tiers.get(tier) or {}
        model = (data.get("model") or "").strip()
        if not model:
            raise ModelsStoreError(f"{tier}: model slug required")
        parsed[tier] = {
            "model": model,
            "price_in_per_mtok": _num(data.get("price_in"), f"{tier} price in"),
            "price_out_per_mtok": _num(data.get("price_out"), f"{tier} price out"),
            "max_daily_usd": _num(data.get("max_daily_usd"), f"{tier} daily cap"),
        }
    if caps.get("on_cap") not in ("rule-only", "fail", "skip"):
        raise ModelsStoreError("on_cap must be rule-only | fail | skip")
    caps_out = {
        "monthly_usd": _num(caps.get("monthly_usd"), "monthly cap"),
        "on_cap": caps["on_cap"],
    }

    def _mutate(lines):
        for tier, vals in parsed.items():
            region = _region(lines, tier)
            if region is None:
                raise cs.StructureError(f"no tiers.{tier} block")
            s, e = region
            cs.require(_set_key(lines, "model", vals["model"],
                                start=s, end=e), f"{tier}.model")
            cs.require(_set_key(lines, "price_in_per_mtok",
                                str(vals["price_in_per_mtok"]),
                                start=s, end=e), f"{tier}.price_in")
            cs.require(_set_key(lines, "price_out_per_mtok",
                                str(vals["price_out_per_mtok"]),
                                start=s, end=e), f"{tier}.price_out")
            cs.require(_set_key(lines, "max_daily_usd",
                                str(vals["max_daily_usd"]),
                                start=s, end=e), f"{tier}.cap")
        caps_region = _region(lines, "caps")
        if caps_region is None:
            raise cs.StructureError("no caps block")
        s, e = caps_region
        cs.require(_set_key(lines, "monthly_usd",
                            str(caps_out["monthly_usd"]), indent="  ",
                            start=s, end=e), "caps.monthly_usd")
        cs.require(_set_key(lines, "on_cap", caps_out["on_cap"],
                            indent="  ", start=s, end=e), "caps.on_cap")

    def _validate():
        cfg = core_config.load_models_cfg()
        for tier in TIERS:
            if cfg.tiers[tier].model != parsed[tier]["model"]:
                raise core_config.ConfigError(f"{tier} round-trip mismatch")
        return {"tiers": cfg.tiers, "caps": cfg.caps}

    return cs.commit(_path(), _mutate, _validate, error_cls=ModelsStoreError,
                     structure_msg="models.yaml structure not recognised "
                     "— keys should sit at four-space indent inside "
                     "tiers:/caps:")


# ── provider price refresh ───────────────────────────────────────────────────


def refresh_prices() -> dict:
    """Fetch GET {base_url}/models and update tier prices where the
    provider reports them (OpenRouter-style pricing.prompt/completion,
    per-token USD). Local caps/policies are untouched. Slugs are matched
    exactly, then by suffix (vendor/model)."""
    import os

    import httpx

    env = {**os.environ, **core_config.load_env()}
    settings = core_config.load_settings()
    base = (env.get(settings.llm.base_url_env) or "").strip().rstrip("/")
    key = (env.get(settings.llm.api_key_env) or "").strip()
    if not base:
        return {"error": "no base URL configured (set "
                         f"{settings.llm.base_url_env} in .env)"}
    url = base + "/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception as e:
        return {"error": f"provider fetch failed: {e}"}

    prices: dict[str, tuple[float, float]] = {}
    for m in data:
        mid = m.get("id")
        pricing = m.get("pricing") or {}
        try:
            pin = float(pricing.get("prompt"))
            pout = float(pricing.get("completion"))
        except (TypeError, ValueError):
            continue
        prices[mid] = (round(pin * 1_000_000, 4), round(pout * 1_000_000, 4))
    if not prices:
        return {"error": "the provider's /models endpoint returned no "
                         "per-token pricing (only OpenRouter-style APIs "
                         "publish it) — set prices by hand below"}

    cfg = current()
    updated, missing = [], []
    path = _path()
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    for tier in TIERS:
        slug = cfg.tiers[tier].model
        if slug in prices:
            pin, pout = prices[slug]
        else:
            suffix = [p for p in prices if p.endswith("/" + slug.split("/")[-1])]
            if len(suffix) == 1:
                pin, pout = prices[suffix[0]]
            else:
                missing.append(slug)
                continue
        region = _region(lines, tier)
        if region is None:
            continue
        s, e = region
        _set_key(lines, "price_in_per_mtok", str(pin), start=s, end=e)
        _set_key(lines, "price_out_per_mtok", str(pout), start=s, end=e)
        updated.append(f"{tier}: {slug} → ${pin}/${pout} per mtok")
    if not updated:
        return {"error": "no tier model matched the provider's list",
                "missing": missing}
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        core_config.load_models_cfg()
    except Exception:
        path.write_text(original, encoding="utf-8")
        return {"error": "refreshed file failed validation — restored"}
    return {"updated": updated, "missing": missing,
            "available": len(prices)}
