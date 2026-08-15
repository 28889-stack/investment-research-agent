from app.fundamental.deep_research import build_deep_research_task_cards
from app.fundamental.schemas import LeadReviewOutput


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
