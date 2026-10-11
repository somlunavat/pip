"""Post-processing for synthesized answers: which URLs may be opened in the browser.

The model proposes URLs, but web content it read could try to steer it toward a
hostile link. A URL is opened only if it is http(s) and its site is one of the
sources the answer came from, or a well-known search or booking site where the
model built a prefilled search (e.g. Google Flights for "cheapest flight to Tokyo").
"""

from __future__ import annotations

from urllib.parse import urlsplit

from orchestrator.models import Answer, MilestoneResult

MAX_OPEN = 3

SEARCH_SITES = {
    "google.com", "bing.com", "duckduckgo.com", "youtube.com", "wikipedia.org",
    "amazon.com", "ebay.com", "bestbuy.com", "target.com", "walmart.com",
    "kayak.com", "expedia.com", "booking.com", "airbnb.com", "skyscanner.com",
    "zillow.com", "apartments.com", "yelp.com", "opentable.com", "maps.apple.com",
    "github.com", "news.ycombinator.com", "reddit.com",
}


def _site(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower().removeprefix("www.")


def _allowed(site: str, source_sites: set[str]) -> bool:
    if site in source_sites:
        return True
    return any(site == s or site.endswith("." + s) for s in SEARCH_SITES)


def clean_answer(answer: Answer, results: list[MilestoneResult]) -> Answer:
    """Drop row links and open URLs that are not http(s) or not from a trusted site."""
    source_sites = {s for r in results for src in r.sources if (s := _site(src.url))}

    opens: list[str] = []
    for url in answer.open_urls:
        site = _site(url)
        if site and _allowed(site, source_sites) and url not in opens:
            opens.append(url.strip())
    for row in answer.rows:
        if row.url and not ((site := _site(row.url)) and _allowed(site, source_sites)):
            row.url = None
    return answer.model_copy(update={"open_urls": opens[:MAX_OPEN], "rows": answer.rows[:8]})
