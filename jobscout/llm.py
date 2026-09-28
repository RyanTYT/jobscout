"""LLM client — OpenAI-compatible, three tiers, spend metered, caps enforced.

Design (PLAN §1, §9):
  * One API key authorizes every tier; tiers differ by model + daily cap.
  * Every call is metered into the llm_calls table (tokens + computed cost).
  * Per-tier daily caps raise CapExceeded BEFORE a call — callers degrade
    gracefully (rule-only scoring), never crash.
  * No key -> available=False; callers skip LLM work entirely.
  * JSON mode is requested but retried without it for providers that
    reject response_format.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

from jobscout.core import db
from jobscout.core.config import load_env, load_models_cfg, load_settings
from jobscout.core.models import TierCfg

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LlmError(Exception):
    """LLM call failed (network, auth, payload)."""


class CapExceeded(LlmError):
    """Tier daily spend cap reached — stop calling, degrade gracefully."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LlmResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    tool_calls: list[ToolCall] | None = None


class LlmClient:
    def __init__(self, *, conn=None, settings=None, models_cfg=None, env=None):
        self.settings = settings or load_settings()
        self.models = models_cfg or load_models_cfg()
        if env is None:
            env = {**os.environ, **load_env()}
        self.api_key = (env.get(self.settings.llm.api_key_env) or "").strip()
        self.base_url = (
            (env.get(self.settings.llm.base_url_env) or "").strip().rstrip("/")
            or DEFAULT_BASE_URL
        )
        self._conn = conn

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ── tiers ───────────────────────────────────────────────────────────────

    def tier_cfg(self, tier: str) -> TierCfg:
        if tier not in self.models.tiers:
            raise LlmError(f"unknown tier {tier!r} — check config/models.yaml")
        return self.models.tiers[tier]

    # ── spend metering ──────────────────────────────────────────────────────

    def daily_spend(self, tier: str) -> float:
        conn = self._conn if self._conn is not None else _try_connect()
        if conn is None:
            return 0.0
        own = self._conn is None
        try:
            return daily_spend(conn, tier)
        finally:
            if own:
                conn.close()

    # ── chat ────────────────────────────────────────────────────────────────

    def chat(
        self,
        tier: str,
        messages: list[dict],
        *,
        json_mode: bool = True,
        cache_key: str | None = None,
        tools: list[dict] | None = None,
    ) -> LlmResponse:
        cfg = self.tier_cfg(tier)
        if not self.available:
            raise LlmError("no API key set (JOBSCOUT_LLM_API_KEY in .env)")

        spent = self.daily_spend(tier)
        if spent >= cfg.max_daily_usd:
            raise CapExceeded(
                f"tier {tier!r} daily cap ${cfg.max_daily_usd:.2f} reached "
                f"(spent ${spent:.4f}) — degrading to rule-only"
            )

        payload: dict = {
            "model": cfg.model,
            "messages": messages,
            "temperature": 0,
        }
        if json_mode and not tools:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        last_err: Exception | None = None
        for attempt in range(3):  # retry: transient 5xx/429, then JSON-mode fallback
            try:
                with httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                    r = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2.5 * (attempt + 1))
                continue
            if r.status_code == 400 and json_mode and "response_format" in payload:
                # provider rejected JSON mode — retry once without it
                payload.pop("response_format", None)
                continue
            if r.status_code != 200:
                raise LlmError(f"{cfg.model}: HTTP {r.status_code}: {r.text[:300]}")
            try:
                data = r.json()
                message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LlmError(f"{cfg.model}: unexpected response shape: {e}") from e
            text = message.get("content") or ""
            tool_calls: list[ToolCall] = []
            for tc in message.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.get("id") or "",
                                           name=fn.get("name") or "",
                                           arguments=args))
            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            cost = _cost(cfg, pt, ct)
            self._record_call(tier, cfg.model, cache_key, pt, ct, cost)
            return LlmResponse(text=text, model=cfg.model, prompt_tokens=pt,
                               completion_tokens=ct, cost_usd=cost,
                               tool_calls=tool_calls or None)
        raise LlmError(f"{cfg.model}: request failed after retries: {last_err}")

    # ── internals ───────────────────────────────────────────────────────────

    def _record_call(self, tier: str, model: str, cache_key, pt: int, ct: int,
                     cost: float) -> None:
        conn = self._conn if self._conn is not None else _try_connect()
        if conn is None:
            return  # metering is best-effort
        own = self._conn is None
        try:
            record_llm_call(conn, tier=tier, model=model, cache_key=cache_key,
                            prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)
        except Exception:  # noqa: BLE001 — never fail a call because metering failed
            pass
        finally:
            if own:
                conn.close()


def _cost(cfg: TierCfg, prompt_tokens: int, completion_tokens: int) -> float:
    pin = cfg.price_in_per_mtok or 0.0
    pout = cfg.price_out_per_mtok or 0.0
    return round(prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout, 6)


# ── standalone helpers over a caller-owned connection ───────────────────────


def _try_connect():
    try:
        return db.connect()
    except FileNotFoundError:
        return None


def daily_spend(conn, tier: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls "
        "WHERE tier = ? AND date(created_at) = date('now')",
        (tier,),
    ).fetchone()
    return float(row["s"] or 0.0)


def record_llm_call(conn, *, tier: str, model: str, cache_key: str | None,
                    prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
    conn.execute(
        "INSERT INTO llm_calls (tier, model, cache_key, prompt_tokens, completion_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tier, model, cache_key, prompt_tokens, completion_tokens, cost_usd),
    )
    conn.commit()
