from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ResearchRole = Literal["business", "industry"]


@dataclass
class ResearchRound:
    round_index: int
    query: str
    result_urls: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)


@dataclass
class ResearchLoopAudit:
    role: ResearchRole
    company_name: str
    rounds: list[ResearchRound] = field(default_factory=list)


class SpecialistResearchLoop:
    """Small, deterministic coordinator for a Specialist's two search rounds.

    The Agent still selects sources and writes the research brief.  This helper
    owns the role-specific query frame and round limit. Reading a source is
    optional, so an unreadable or low-value result cannot block the next search.
    """

    max_rounds = 2

    def __init__(
        self,
        *,
        role: ResearchRole,
        company_name: str,
        business_scope: list[str],
        industry_scope: list[str],
    ) -> None:
        self.role = role
        self.company_name = company_name
        self.business_scope = [item.strip() for item in business_scope if item.strip()]
        self.industry_scope = [item.strip() for item in industry_scope if item.strip()]
        self.audit = ResearchLoopAudit(role=role, company_name=company_name)
        self.stop_reason: str | None = None

    def start_query(self, proposed: str | None = None) -> str:
        if self.audit.rounds:
            raise ValueError("首轮查询已生成")
        return self._open_round(self._compose_query(proposed=proposed, unresolved=[]))

    def record_search(self, query: str, result_urls: list[str]) -> None:
        current = self._current_round()
        if current.query != query:
            raise ValueError("搜索词必须使用当前轮次改写结果")
        if current.result_urls:
            raise ValueError("同一轮次只能搜索一次")
        current.result_urls = list(dict.fromkeys(url for url in result_urls if url))

    def record_read(
        self, evidence_id: str, claim: str, *, round_index: int | None = None
    ) -> None:
        current = (
            self.audit.rounds[round_index - 1]
            if round_index is not None and 1 <= round_index <= len(self.audit.rounds)
            else self._current_round()
        )
        if not current.result_urls:
            raise ValueError("必须先完成检索再读取来源")
        if evidence_id not in current.evidence_ids:
            current.evidence_ids.append(evidence_id)
            current.claims.append(claim)
        if len(self.audit.rounds) >= self.max_rounds:
            self.stop_reason = "max_rounds_reached"

    def next_query(self, unresolved: list[str]) -> str:
        self._current_round()
        if len(self.audit.rounds) >= self.max_rounds:
            self.stop_reason = "max_rounds_reached"
            raise ValueError("检索轮次已达上限")
        return self._open_round(self._compose_query(unresolved=unresolved))

    def _open_round(self, query: str) -> str:
        self.audit.rounds.append(ResearchRound(round_index=len(self.audit.rounds) + 1, query=query))
        return query

    def _current_round(self) -> ResearchRound:
        if not self.audit.rounds:
            raise ValueError("尚未生成检索查询")
        return self.audit.rounds[-1]

    def _compose_query(self, *, proposed: str | None = None, unresolved: list[str]) -> str:
        if self.role == "business":
            scope = self.business_scope or ["商业模式", "核心资产", "经营执行"]
            offset = len(self.audit.rounds) % len(scope)
            focus = [*scope[offset : offset + 3], *scope[: max(0, offset + 3 - len(scope))]]
            additions = unresolved[:2]
            return " ".join([self.company_name, *focus, *additions])

        # Industry intentionally does not preserve a company-event query.  The
        # company is only the source of commodity/region exposure, never the
        # query's primary subject.
        del proposed
        exposure = self.industry_scope or ["上游供给", "下游需求"]
        offset = len(self.audit.rounds) % len(exposure)
        selected = [exposure[offset], *exposure[offset + 1 : offset + 2]]
        additions = unresolved[:2]
        return " ".join([*selected, "全球供需", "价格", "成本", "政策", *additions])
