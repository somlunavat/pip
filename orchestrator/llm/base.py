"""The model interface the orchestrator depends on.

Production uses GeminiClient; tests pass a fake that implements the same methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from orchestrator.models import Answer, MilestoneResult, Plan


@dataclass
class Usage:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    cost_usd: float | None = None


@dataclass
class Triage:
    small_talk: bool
    reply: str = ""


class LLM(Protocol):
    async def triage(self, text: str, context: dict[str, str]) -> Triage: ...

    async def plan(
        self,
        text: str,
        context: dict[str, str],
        *,
        prior: list[MilestoneResult] | None = None,
        failed: str | None = None,
    ) -> Plan: ...

    async def synthesize(self, text: str, results: list[MilestoneResult],
                         location: str = "") -> Answer: ...

    async def extract_fields(self, content: str, extractor: str, source: str) -> dict[str, Any]: ...

    async def transcribe(self, audio: bytes, mime_type: str) -> str: ...

    async def embed(self, text: str) -> np.ndarray: ...
