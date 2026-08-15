from app.fundamental.deep_research import (
    build_deep_research_task_cards,
    normalize_deep_query_plan,
)
from app.fundamental.schemas import DeepResearchQueryPlan, LeadReviewOutput


def test_build_deep_research_task_cards_groups_review_gaps_and_caps_at_three() -> None:
    review = LeadReviewOutput(
        symbol="600519.SH",
        business_status="accepted",
        industry_status="accepted_with_gaps",
        key_findings=["品牌与周期并存"],
        conflicts=["增长持续性仍待验证"],
        financial_questions=["自由现金流能否覆盖资本开支"],
        missing_information=["海外渠道收入缺少拆分", "行业库存数据口径不统一"],
        followup_research_tasks=["核验自由现金流与资本开支关系", "梳理行业库存与价格变化"],
    )

    cards = build_deep_research_task_cards(review)

    assert 1 <= len(cards) <= 3
    assert [card.task_id for card in cards] == [f"deep_{index:02d}" for index in range(1, len(cards) + 1)]
    assert any("自由现金流" in question for card in cards for question in card.research_questions)
    assert all(card.known_material == review.key_findings for card in cards)
    assert all(card.excluded_claims == review.conflicts for card in cards)


def test_normalize_deep_query_plan_covers_each_card_with_at_most_two_queries() -> None:
    review = LeadReviewOutput(
        symbol="603871.SH",
        business_status="accepted",
        industry_status="accepted_with_gaps",
        key_findings=["跨境物流业务"],
        conflicts=[],
        financial_questions=[],
        missing_information=[],
        followup_research_tasks=[],
        deep_research_tasks=[
            {
                "task_id": "deep_01",
                "topic": "蒙古焦煤物流兑现",
                "scope": "业务量和利润",
                "research_questions": ["过货量", "利润率"],
                "priority_fact_types": [],
                "known_material": [],
                "excluded_claims": [],
            },
            {
                "task_id": "deep_02",
                "topic": "非洲铜矿物流",
                "scope": "项目进度",
                "research_questions": ["卡莫阿项目进度"],
                "priority_fact_types": [],
                "known_material": [],
                "excluded_claims": [],
            },
        ],
    )
    cards = build_deep_research_task_cards(review)
    plan = DeepResearchQueryPlan(
        symbol="603871.SH",
        queries=[
            {"task_id": "deep_01", "queries": ["嘉友国际 蒙古 焦煤 过货量", "嘉友国际 蒙古焦煤 利润率"]},
        ],
    )

    normalized = normalize_deep_query_plan(plan, cards, "603871.SH")

    assert [item.task_id for item in normalized] == ["deep_01", "deep_02"]
    assert normalized[0].queries == plan.queries[0].queries
    assert len(normalized[1].queries) == 1
    assert "非洲铜矿物流" in normalized[1].queries[0]
