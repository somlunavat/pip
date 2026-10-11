"""Post-processing for synthesized answers: which URLs may be opened in the browser.

The model proposes URLs, but web content it read could try to steer it toward a
hostile link. A URL is opened only if it is http(s) and its site is one of the
sources the answer came from, or a well-known search or booking site where the
model built a prefilled search (e.g. Google Flights for "cheapest flight to Tokyo").
"""

from __future__ import annotations

import re
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


# The user asked to see pages, not just get an answer. Phrased as commands so that
# "is the store open on Sunday?" or "show me the cheapest flight" don't open tabs.
_WANTS_BROWSER = re.compile(
    r"^\s*(please\s+)?(open|pull up|bring up|take me to|go to|load)\b"
    r"|\b(and|then|also)\s+(open|pull up|bring up)\b"
    r"|\b(open|pull up|bring up)\s+(it|them|that|those|these|the\s+(page|pages|site|sites|link"
    r"|links|result|results|tab|tabs|listings?|options?))\b"
    r"|\b(in|on)\s+(a\s+|my\s+|the\s+)?(new\s+)?(browser|chrome|safari|tabs?)\b",
    re.IGNORECASE,
)


def wants_browser(request: str) -> bool:
    """Whether the request asks for pages to be opened (deterministic, not the model)."""
    return bool(_WANTS_BROWSER.search(request))


def should_open(mode: str, request: str) -> bool:
    """mode: 'ask' (default) opens only when the request asks; 'always'; 'never'."""
    if mode == "never":
        return False
    return mode == "always" or wants_browser(request)


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
