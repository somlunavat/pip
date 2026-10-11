"""Test doubles for the LLM protocol."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from orchestrator.llm.base import Triage
from orchestrator.models import Answer, AnswerRow, MilestoneResult, Plan


class FakeLLM:
    def __init__(self, plans: list[dict[str, Any]] | None = None, *, small_talk: str = "",
                 fields: list[dict[str, Any]] | None = None, dim: int = 8,
                 open_urls: list[str] | None = None):
        self.plans = list(plans or [])
        self.small_talk = small_talk
        self.fields = list(fields or [])
        self.dim = dim
        self.open_urls = list(open_urls or [])
        self.plan_calls: list[dict[str, Any]] = []
        self.synth_inputs: list[list[MilestoneResult]] = []
        self.vectors: dict[str, np.ndarray] = {}

    async def triage(self, text: str, context: dict[str, str]) -> Triage:
        return Triage(small_talk=bool(self.small_talk), reply=self.small_talk)

    async def plan(self, text: str, context: dict[str, str], *, prior=None, failed=None) -> Plan:
        self.plan_calls.append({"text": text, "prior": prior, "failed": failed,
                                "problems": context.get("plan_problems")})
        return Plan.model_validate(self.plans.pop(0))

    async def synthesize(self, text: str, results: list[MilestoneResult],
                         location: str = "") -> Answer:
        self.synth_inputs.append(results)
        self.synth_location = location
        return Answer(
            headline="answer: " + " | ".join(f"{r.milestone_id}={r.ok}" for r in results),
            rows=[AnswerRow(label=s.url, value="x", url=s.url) for r in results
                  for s in r.sources],
            open_urls=self.open_urls,
        )

    async def extract_fields(self, content: str, extractor: str, source: str) -> dict[str, Any]:
        return self.fields.pop(0)

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        return audio.decode()

    async def embed(self, text: str) -> np.ndarray:
        """Deterministic pseudo-embedding, or a preset vector if one is registered."""
        if text in self.vectors:
            return self.vectors[text]
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        vec = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return vec / np.linalg.norm(vec)
