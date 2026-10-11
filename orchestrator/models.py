"""Plan and result types shared by the planner, router, executor, and verifier."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Kind = Literal["read", "work", "local", "watch"]
Path = Literal["nimble", "worker", "chrome", "mac", "watcher", "none"]


class WatchSpec(BaseModel):
    target: str
    extractor: str = Field(description="Fields to track, e.g. 'price, status, listing count'")
    interval_s: int = 900
    threshold: float = 0.35


class Milestone(BaseModel):
    id: str
    goal: str
    kind: Kind
    success_signal: str = ""
    depends_on: list[str] = Field(default_factory=list)
    # read: a search query and/or a URL to extract
    query: str | None = None
    url: str | None = None
    # work: why this must run on a worker (login, form, file, code, long job)
    needs_login: bool = False
    submits_form: bool = False
    writes_file: bool = False
    runs_code: bool = False
    estimated_seconds: int = 10
    # local: one allowlisted Mac tool
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    # watch
    watch: WatchSpec | None = None

    @field_validator("depends_on", mode="before")
    @classmethod
    def _none_to_list(cls, v: Any) -> Any:
        return v or []


class Plan(BaseModel):
    milestones: list[Milestone]
    summary: str = ""

    def by_id(self) -> dict[str, Milestone]:
        return {m.id: m for m in self.milestones}


class Source(BaseModel):
    url: str
    title: str = ""


class MilestoneResult(BaseModel):
    milestone_id: str
    ok: bool
    path: Path
    text: str = ""
    sources: list[Source] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    tier: int = 0
