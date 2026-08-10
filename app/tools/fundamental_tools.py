from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.fundamental.data import get_company_profile, get_financial_data, get_market_snapshot
from app.fundamental.evidence import (
    EvidenceStore,
    ResearchSourceError,
    read_research_source,
    search_research_sources,
)
from app.fundamental.financials import calculate_financial_metrics
from app.fundamental.schemas import (
    AssumptionStore,
    CompanyProfile,
    EvidenceItem,
    FinancialData,
    FinancialMetrics,
    ResearchSearchResults,
    ResearchSource,
    ValuationResult,
)
from app.fundamental.valuation import calculate_valuation
from app.run_service import RunService
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import ToolExecutionContext
from app.runtime.tool_registry import ToolDefinition, ToolRegistry


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    # Optional source-kind preferences. The aggregator always invokes every
    # configured Provider; these values only reorder results by preferred kinds.
    sources: list[str] = Field(
        default_factory=list,
        description="可选来源偏好（announcement、research_report、news、web）；只影响排序，不限制检索 Provider。",
    )


class ReadSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str
    claim: str = Field(min_length=1, max_length=2_000)
    evidence_type: Literal[
        "historical_fact",
        "management_statement",
        "third_party_forecast",
        "analyst_estimate",
    ]

class ReadSourceOutput(BaseModel):
    evidence_id: str
    source_name: str


def _atomic_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def build_fundamental_tools(
    registry: ToolRegistry,
    run_service: RunService,
    repository: RuntimeRepository,
    settings: Settings,
) -> ToolRegistry:
    del repository
    source_cache: dict[str, dict[str, ResearchSource]] = {}
    # read_guard[run_id] = {"result_id": remaining_reads}. Tracks the
    # per-result_id read allowance AND the per-search read cap so an agent
    # cannot burn its 10-call tool budget on duplicate reads of the same
    # result_id (a real failure mode observed against 紫金矿业: src_002 read
    # twice) nor over-read a single search's results.
    read_guard: dict[str, dict[str, int]] = {}
    _MAX_READS_PER_RESULT_ID = 1
    _MAX_READS_PER_SEARCH = 4

    def directory(run_id: str) -> Path:
        path = settings.artifacts_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def company_handler(_args: BaseModel, context: ToolExecutionContext):
        run = run_service.get_run(context.run_id)
        profile = get_company_profile(run.resolved_symbol or "", _as_date(run.as_of), settings)
        _atomic_model(profile, directory(run.run_id) / "company_profile.json")
        return profile.model_dump(mode="json")

    def search_handler(args: BaseModel, context: ToolExecutionContext):
        assert isinstance(args, SearchInput)
        run = run_service.get_run(context.run_id)
        result = search_research_sources(
            args.query,
            run.resolved_symbol or "",
            settings,
            sources=args.sources or None,
        )
        source_cache[run.run_id] = {item.result_id: item for item in result.items}
        # Reset the read guard for the freshly-renumbered result_id namespace
        # so result_ids stay scoped to THIS search only. Without the reset,
        # a stale allowance for src_002 from an earlier search would let the
        # agent re-read a *different* source that happened to inherit the id.
        read_guard[run.run_id] = {}
        return result.model_dump(mode="json")

    def read_handler(args: BaseModel, context: ToolExecutionContext):
        assert isinstance(args, ReadSourceInput)
        source = source_cache.get(context.run_id, {}).get(args.result_id)
        if source is None:
            raise ValueError("搜索结果不存在或不属于当前任务")
        guard = read_guard.setdefault(context.run_id, {})
        already = guard.get(args.result_id, 0)
        total = sum(guard.values())
        if already >= _MAX_READS_PER_RESULT_ID:
            raise ResearchSourceError(
                f"RESEARCH_SOURCE_FAILED: 该搜索结果 {args.result_id} 已被读取，不得重复读取；请改读其他 result_id 或输出结果"
            )
        if total >= _MAX_READS_PER_SEARCH:
            raise ResearchSourceError(
                "RESEARCH_SOURCE_FAILED: 本轮搜索结果读取已达上限，请基于已生成的 Evidence 输出结果，不要再调用 read_research_source"
            )
        item = read_research_source(
            source,
            claim=args.claim,
            evidence_type=args.evidence_type,
            store=EvidenceStore(directory(context.run_id) / "evidence.json"),
            settings=settings,
        )
        guard[args.result_id] = already + 1
        return {"evidence_id": item.id, "source_name": item.source_name}

    def financial_data_handler(_args: BaseModel, context: ToolExecutionContext):
        run = run_service.get_run(context.run_id)
        result = get_financial_data(run.resolved_symbol or "", _as_date(run.as_of), settings)
        _atomic_model(result, directory(run.run_id) / "financial_data.json")
        return result.model_dump(mode="json")

    def metrics_handler(_args: BaseModel, context: ToolExecutionContext):
        path = directory(context.run_id)
        data = FinancialData.model_validate_json((path / "financial_data.json").read_text(encoding="utf-8"))
        result = calculate_financial_metrics(data, settings.financial_metric_version)
        _atomic_model(result, path / "financial_metrics.json")
        return result.model_dump(mode="json")

    def valuation_handler(_args: BaseModel, context: ToolExecutionContext):
        path = directory(context.run_id)
        run = run_service.get_run(context.run_id)
        data = FinancialData.model_validate_json((path / "financial_data.json").read_text(encoding="utf-8"))
        metrics = FinancialMetrics.model_validate_json((path / "financial_metrics.json").read_text(encoding="utf-8"))
        assumptions = AssumptionStore.model_validate_json((path / "assumptions.json").read_text(encoding="utf-8"))
        snapshot = get_market_snapshot(run.resolved_symbol or "", _as_date(run.as_of), settings)
        result = calculate_valuation(data, metrics, assumptions, snapshot, settings.valuation_script_version)
        _atomic_model(result, path / "valuation_result.json")
        return result.model_dump(mode="json")

    definitions = [
        ToolDefinition("get_company_profile", "获取当前任务的公司基础资料", EmptyInput, CompanyProfile, {"full"}, {"fundamental_lead", "business_research"}, settings.tool_default_timeout, "low", True, company_handler),
        ToolDefinition("search_research_sources", "聚合全部已配置公开来源；sources 仅作为结果排序偏好", SearchInput, ResearchSearchResults, {"full"}, {"fundamental_lead", "business_research", "industry_research", "deep_research"}, settings.research_source_timeout, "medium", False, search_handler),
        ToolDefinition("read_research_source", "读取先前搜索结果并生成 Evidence", ReadSourceInput, ReadSourceOutput, {"full"}, {"fundamental_lead", "business_research", "industry_research", "deep_research"}, settings.research_source_timeout, "medium", True, read_handler),
        ToolDefinition("get_financial_data", "获取并保存标准化财务数据", EmptyInput, FinancialData, {"constrained"}, {"financial_research"}, settings.tool_default_timeout, "medium", True, financial_data_handler),
        ToolDefinition("calculate_financial_metrics", "使用 Python 计算财务指标", EmptyInput, FinancialMetrics, {"constrained"}, {"financial_research"}, settings.tool_default_timeout, "low", True, metrics_handler),
        ToolDefinition("calculate_valuation", "使用 Python 计算相对估值和简化 DCF", EmptyInput, ValuationResult, {"constrained"}, {"valuation_research"}, settings.tool_default_timeout, "low", True, valuation_handler),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry


def _as_date(value: str):
    from datetime import date

    return date.fromisoformat(value)
