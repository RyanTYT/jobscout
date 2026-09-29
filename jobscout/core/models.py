"""Pydantic schemas — the executable form of PLAN.md §5.

If a schema changes, change BOTH this file and PLAN.md §5 in the same commit
(see AGENTS.md). IDs in master_resume are stable forever: never renumber.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_YYYY_MM = re.compile(r"^\d{4}-\d{2}$")


def _check_yyyy_mm(v: str | None, field: str) -> str | None:
    if v is None or v == "present" or v == "":
        return v
    if not _YYYY_MM.match(v):
        raise ValueError(f"{field} must be 'YYYY-MM', 'present', or null — got {v!r}")
    return v


# ── master_resume/resume.yaml (PLAN §5.1) ────────────────────────────────────


class Dates(BaseModel):
    start: str
    end: str | None = None  # 'YYYY-MM' | 'present' | None (= present)

    @field_validator("start")
    @classmethod
    def _v_start(cls, v: str) -> str:
        return _check_yyyy_mm(v, "dates.start")  # type: ignore[return-value]

    @field_validator("end")
    @classmethod
    def _v_end(cls, v: str | None) -> str | None:
        return _check_yyyy_mm(v, "dates.end")

    @property
    def is_current(self) -> bool:
        return self.end in (None, "present")


class TitleSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str
    from_: str = Field(alias="from")
    to: str


class LinkExtra(BaseModel):
    id: str
    label: str
    url: str


class Links(BaseModel):
    personal_website: str | None = None
    linkedin: str | None = None
    github: str | None = None
    extra: list[LinkExtra] = Field(default_factory=list)


class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None
    timezone: str | None = None


class Identity(BaseModel):
    full_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: Location = Field(default_factory=Location)
    links: Links = Field(default_factory=Links)


class WorkAuthorization(BaseModel):
    country: str
    status: str | None = None
    sponsorship_needed: bool = False
    note: str | None = None


class Bullet(BaseModel):
    id: str
    text: str
    metrics: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Education(BaseModel):
    id: str
    school: str | None = None
    degree: str | None = None
    field: str | None = None
    dates: Dates | None = None
    gpa: float | None = None
    honors: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    id: str
    company: str
    domain: str | None = None
    context: str | None = None
    title: str | None = None
    titles: list[TitleSpan] = Field(default_factory=list)
    dates: Dates
    location: str | None = None
    employment: Literal["full-time", "contract", "internship"] = "full-time"
    tech: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    url: str | None = None
    dates: Dates | None = None
    role: str | None = None
    context: str | None = None
    tech: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class SkillsArea(BaseModel):
    area: str
    items: list[str] = Field(default_factory=list)


class Publication(BaseModel):
    id: str
    title: str
    venue: str | None = None
    date: str | None = None
    url: str | None = None


class Skills(BaseModel):
    core: list[SkillsArea] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)


class SalaryPolicy(BaseModel):
    policy: str = "defer to conversation"
    floor: float | None = None
    currency: str = "USD"


class ReferralPolicy(BaseModel):
    policy: str = "company careers page"


class EeoPolicy(BaseModel):
    policy: Literal["decline", "provide"] = "decline"


class ExtrasOther(BaseModel):
    id: str
    label: str
    value: str


class Extras(BaseModel):
    salary: SalaryPolicy = Field(default_factory=SalaryPolicy)
    notice_period: str | None = None
    availability: str | None = None
    languages_spoken: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    referrals: ReferralPolicy = Field(default_factory=ReferralPolicy)
    eeo: EeoPolicy = Field(default_factory=EeoPolicy)
    other: list[ExtrasOther] = Field(default_factory=list)


class MasterResume(BaseModel):
    version: int = 3
    identity: Identity = Field(default_factory=Identity)
    work_authorization: list[WorkAuthorization] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    extras: Extras = Field(default_factory=Extras)


# ── config/settings.yaml (PLAN §1.1, §8) ─────────────────────────────────────


class AgentCfg(BaseModel):
    schedule: Literal["daily", "weekdays", "mon-wed-fri", "manual"] = "weekdays"
    max_steps: int = 60
    max_cost_usd: float = 0.50
    model: str = "agent"
    run_on_signal: bool = True


class PipelineCfg(BaseModel):
    ats_boards: bool = True
    careers_crawl: bool = True
    cse_queries_per_day: int = 10
    rss: bool = True
    rss_feeds: list[str] = Field(default_factory=list)  # empty -> rss.DEFAULT_FEEDS


class DiscoveryCfg(BaseModel):
    mode: Literal["off", "pipeline", "agent", "hybrid"] = "hybrid"
    agent: AgentCfg = Field(default_factory=AgentCfg)
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)


class DashboardCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class SidecarCfg(BaseModel):
    enabled: bool = False
    path: str = "../JobPilot"
    command: str = "npm run dev"


class LlmCfg(BaseModel):
    provider: str = "openai-compatible"
    base_url_env: str = "JOBSCOUT_LLM_BASE_URL"
    api_key_env: str = "JOBSCOUT_LLM_API_KEY"


class RunCfg(BaseModel):
    daily_hour: int = 6
    daily_minute: int = 30
    agent_hour: int = 7
    agent_minute: int = 0
    skip_if_run_within_hours: int = 20


class SearchCfg(BaseModel):
    """Which engine powers the agent's web_search tool (agent/tools.py).

    auto: first configured of CSE -> Brave -> LLM-search -> DuckDuckGo.
    llm: a search-grounded chat model (e.g. OpenRouter :online plugins or
    sonar) queried through the existing LLM key — no extra signup, a few
    cents per run; the slug lives in llm_model."""
    provider: Literal["auto", "cse", "brave", "llm", "ddg"] = "auto"
    llm_model: str = ""


class Settings(BaseModel):
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)
    search: SearchCfg = Field(default_factory=SearchCfg)
    dashboard: DashboardCfg = Field(default_factory=DashboardCfg)
    sidecar: SidecarCfg = Field(default_factory=SidecarCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    run: RunCfg = Field(default_factory=RunCfg)


# ── config/models.yaml (PLAN §1 tiers) ───────────────────────────────────────


class TierCfg(BaseModel):
    model: str
    max_daily_usd: float
    purpose: str
    price_in_per_mtok: float | None = None   # USD per 1M input tokens (for the spend meter)
    price_out_per_mtok: float | None = None  # USD per 1M output tokens


class CapsCfg(BaseModel):
    monthly_usd: float = 15.0
    on_cap: str = "rule-only"


class ModelsCfg(BaseModel):
    tiers: dict[str, TierCfg]
    caps: CapsCfg = Field(default_factory=CapsCfg)


# ── config/profile.yaml (PLAN §5.2) ──────────────────────────────────────────


class RemoteCfg(BaseModel):
    allowed: bool = True
    preference: Literal["onsite", "hybrid", "remote"] = "hybrid"


class TargetCfg(BaseModel):
    roles: list[str] = Field(default_factory=list)
    weighting: dict[str, float] = Field(default_factory=dict)
    seniorities: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    # priority overlay on locations: rules treat both lists the same
    # (union), but the agent brief, LLM rubric, and digest emphasise
    # primary first. Empty = all locations equal.
    primary_locations: list[str] = Field(default_factory=list)
    remote: RemoteCfg = Field(default_factory=RemoteCfg)
    domains: list[str] = Field(default_factory=list)
    stack: list[str] = Field(default_factory=list)


class ScoringRubric(BaseModel):
    fit: float = 0.6
    company_quality: float = 0.25
    opportunity: float = 0.15


class CompFloor(BaseModel):
    currency: str = "USD"
    amount: float | None = None


class ProfileCfg(BaseModel):
    profile_version: str = "unversioned"
    target: TargetCfg = Field(default_factory=TargetCfg)
    dealbreakers: list[str] = Field(default_factory=list)
    company_tiers: dict[str, list[Any]] = Field(default_factory=dict)
    scoring_rubric: ScoringRubric = Field(default_factory=ScoringRubric)
    comp_floor: CompFloor | None = None


# ── config/watchlist.yaml (PLAN §3) ──────────────────────────────────────────


class WatchlistEntry(BaseModel):
    name: str
    domain: str | None = None
    ats: dict[str, str] = Field(default_factory=dict)  # provider -> board token
    note: str | None = None
    found_via: str | None = None


class Watchlist(BaseModel):
    A: list[WatchlistEntry] = Field(default_factory=list)
    B: list[WatchlistEntry] = Field(default_factory=list)
    C: list[WatchlistEntry] = Field(default_factory=list)
    candidates: list[WatchlistEntry] = Field(default_factory=list)
