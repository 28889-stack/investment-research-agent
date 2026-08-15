from __future__ import annotations

from app.fundamental.schemas import DeepResearchTaskCard, LeadReviewOutput


def build_deep_research_task_cards(review: LeadReviewOutput) -> list[DeepResearchTaskCard]:
    """Turn Lead Review gaps into a small set of non-overlapping research topics."""
    if review.deep_research_tasks:
        return review.deep_research_tasks[:3]

    candidates = list(dict.fromkeys([
        *review.followup_research_tasks,
        *review.financial_questions,
        *review.missing_information,
    ]))
    cards: list[DeepResearchTaskCard] = []
    for index, question in enumerate(candidates[:3], 1):
        cards.append(DeepResearchTaskCard(
            task_id=f"deep_{index:02d}",
            topic=question[:48],
            scope="围绕该专题补足事实、行业背景或可核验经营资料；不重复首轮泛化结论。",
            research_questions=[question],
            priority_fact_types=["historical_fact", "third_party_forecast"],
            known_material=review.key_findings,
            excluded_claims=review.conflicts,
        ))
    return cards
