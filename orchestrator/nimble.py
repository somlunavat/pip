"""Nimble: the fast path for reading the live web.

Search and Extract are single HTTP round trips from the orchestrator, fanned out
in parallel under a concurrency cap. Endpoints follow Nimble's OpenAPI spec
(https://docs.nimbleway.com/api-reference/openapi.json), base https://sdk.nimbleway.com.
Default rate limit is 83 QPS per key; a 429 carries `retry_after`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from orchestrator.models import Milestone, MilestoneResult, Source


class NimbleError(RuntimeError):
    def __init__(self, message: str, code: str = "nimble_error"):
        super().__init__(message)
        self.code = code


@dataclass
class SearchHit:
    title: str
    url: str
    description: str = ""
    content: str = ""


@dataclass
class Page:
    url: str
    markdown: str = ""
    links: list[str] = field(default_factory=list)
    status_code: int | None = None


class NimbleClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://sdk.nimbleway.com",
        concurrency: int = 4,
        timeout_s: float = 45.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self._sem:
            for attempt in range(self._max_retries + 1):
                try:
                    resp = await self._http.post(path, json=body)
                except httpx.TimeoutException as exc:
                    if attempt < self._max_retries:
                        continue
                    raise NimbleError(f"{path} timed out", "timeout") from exc
                if resp.status_code == 429 and attempt < self._max_retries:
                    await asyncio.sleep(_retry_after(resp, attempt))
                    continue
                if resp.status_code >= 500 and attempt < self._max_retries:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                if resp.status_code >= 400:
                    code = "rate_limited" if resp.status_code == 429 else f"http_{resp.status_code}"
                    raise NimbleError(f"{path} -> {resp.status_code}: {resp.text[:200]}", code)
                return resp.json()
        raise NimbleError(f"{path} exhausted retries", "retries_exhausted")

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        data = await self._post(
            "/v2/search",
            {"query": query, "max_results": max_results, "output_format": "markdown"},
        )
        return [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                description=r.get("description", "") or "",
                content=r.get("content", "") or "",
            )
            for r in data.get("results", [])
            if r.get("url")
        ]

    async def extract(self, url: str, *, render: bool | str = "auto") -> Page:
        data = await self._post(
            "/v2/extract",
            {"url": url, "formats": ["markdown", "links"], "render": render,
             "markdown_backend": "main_content"},
        )
        payload = data.get("data") or {}
        return Page(
            url=data.get("url", url),
            markdown=payload.get("markdown", "") or "",
            links=payload.get("links", []) or [],
            status_code=data.get("status_code"),
        )

    async def read(self, m: Milestone) -> MilestoneResult:
        """Run a `read` milestone: search, extract, or both in parallel."""
        start = time.perf_counter()
        jobs: list[Any] = []
        if m.query:
            jobs.append(self.search(m.query))
        if m.url:
            jobs.append(self.extract(m.url))
        if not jobs:
            jobs.append(self.search(m.goal))
        try:
            outputs = await asyncio.gather(*jobs)
        except NimbleError as exc:
            return MilestoneResult(
                milestone_id=m.id, ok=False, path="nimble", error=exc.code,
                latency_ms=_ms(start), tier=1,
            )

        texts: list[str] = []
        sources: list[Source] = []
        for out in outputs:
            if isinstance(out, Page):
                if out.markdown:
                    texts.append(f"# {out.url}\n{out.markdown}")
                    sources.append(Source(url=out.url))
            else:
                for hit in out:
                    texts.append(f"## {hit.title}\n{hit.url}\n{hit.content or hit.description}")
                    sources.append(Source(url=hit.url, title=hit.title))
        text = "\n\n".join(texts)
        return MilestoneResult(
            milestone_id=m.id,
            ok=bool(text.strip()),
            path="nimble",
            text=text,
            sources=sources,
            error=None if text.strip() else "empty_result",
            latency_ms=_ms(start),
            tier=1,
        )


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.json().get("retry_after", 1)), 10.0)
    except (ValueError, AttributeError):
        return min(float(resp.headers.get("retry-after", 1 + attempt)), 10.0)


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000
