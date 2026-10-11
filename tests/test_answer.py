from __future__ import annotations

from orchestrator.answer import clean_answer
from orchestrator.models import Answer, AnswerRow, MilestoneResult, Source

RESULTS = [MilestoneResult(milestone_id="m1", ok=True, path="nimble",
                           sources=[Source(url="https://www.ana.co.jp/fares")])]


def test_keeps_source_sites_and_search_sites_only() -> None:
    answer = Answer(headline="h", open_urls=[
        "https://ana.co.jp/book",                         # same site as a source
        "https://www.google.com/travel/flights?q=tokyo",  # known search site
        "https://evil.example/login",                     # not a source, not a search site
        "javascript:alert(1)",
        "https://ana.co.jp/book",                         # duplicate
        "https://www.kayak.com/flights",
    ])
    cleaned = clean_answer(answer, RESULTS)
    assert cleaned.open_urls == ["https://ana.co.jp/book",
                                 "https://www.google.com/travel/flights?q=tokyo",
                                 "https://www.kayak.com/flights"]


def test_lookalike_hosts_are_not_trusted() -> None:
    answer = Answer(headline="h", open_urls=["https://google.com.evil.example/x",
                                             "https://notgoogle.com/x"])
    assert clean_answer(answer, RESULTS).open_urls == []


def test_untrusted_row_links_are_dropped_but_rows_kept() -> None:
    answer = Answer(headline="h", rows=[AnswerRow(label="a", value="1", url="https://evil.example"),
                                        AnswerRow(label="b", value="2", url="https://ana.co.jp/x")])
    rows = clean_answer(answer, RESULTS).rows
    assert [r.url for r in rows] == [None, "https://ana.co.jp/x"]


def test_markdown_fallback() -> None:
    md = Answer(headline="Cheapest is $742", summary="4 sources",
                rows=[AnswerRow(label="ana", value="$742", url="https://ana.co.jp")]).to_markdown()
    assert md == "**Cheapest is $742**\n4 sources\n- [ana](https://ana.co.jp): $742"
