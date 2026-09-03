from __future__ import annotations

import pytest

from app.fundamental.research_loop import SpecialistResearchLoop


def test_business_loop_keeps_company_as_the_primary_research_subject() -> None:
    loop = SpecialistResearchLoop(
        role="business",
        company_name="紫金矿业",
        business_scope=["矿山项目", "产能投放", "并购整合"],
        industry_scope=["铜", "黄金", "锂"],
    )

    query = loop.start_query()

    assert "紫金矿业" in query
    assert "矿山项目" in query
    assert "全球铜矿供给" not in query


def test_industry_loop_rewrites_company_event_query_to_external_industry_subject() -> None:
    loop = SpecialistResearchLoop(
        role="industry",
        company_name="紫金矿业",
        business_scope=["矿山项目"],
        industry_scope=["铜", "黄金", "锂"],
    )

    query = loop.start_query("紫金矿业半年报 铜金产量 Allied Gold")

    assert "紫金矿业" not in query
    assert any(term in query for term in ("铜", "黄金", "锂"))
    assert any(term in query for term in ("供需", "价格", "成本", "政策"))
    assert "Allied Gold" not in query


def test_loop_allows_a_second_search_without_a_read_and_stops_after_two_rounds() -> None:
    loop = SpecialistResearchLoop(
        role="industry",
        company_name="紫金矿业",
        business_scope=[],
        industry_scope=["铜"],
    )

    first = loop.start_query()
    loop.record_search(first, ["https://example.com/first"])
    second = loop.next_query(["成本曲线"])
    assert second
    loop.record_search(second, ["https://example.com/second"])
    with pytest.raises(ValueError, match="上限"):
        loop.next_query(["第三轮"])

    assert loop.stop_reason == "max_rounds_reached"
    assert [round_.round_index for round_ in loop.audit.rounds] == [1, 2]
    assert loop.audit.rounds[1].query != loop.audit.rounds[0].query
