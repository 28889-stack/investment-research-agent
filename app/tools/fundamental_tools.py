from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.fundamental.data import get_company_profile, get_financial_data, get_market_snapshot
from app.fundamental.evidence import (
    EvidenceStore,
    ResearchSourceError,
    canonical_source_url,
    read_research_source,
    search_research_sources,
)
from app.fundamental.financials import calculate_financial_metrics
from app.fundamental.findkg import FinDKGFocus, FinDKGQueryResult, LocalFinDKG
from app.fundamental.research_loop import SpecialistResearchLoop
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


_monotonic = time.monotonic
_DEEP_TOOL_PHASE_SECONDS = 180.0
_MAX_SEARCH_SUMMARY_CHARS = 800


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
    task_card_id: str | None = Field(
        default=None,
        pattern=r"^deep_\d{2}$",
        description="Deep Research 专题任务卡标识；仅 Deep 节点使用。",
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
    evidence_id: str | None = None
    source_name: str | None = None
    retrieval_notice: str | None = None


class QueryFinDKGInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[str] = Field(min_length=1, max_length=8)
    focus: FinDKGFocus = "general"
    max_hops: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=15, ge=5, le=30)


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
    result_read_scope: dict[str, dict[str, str]] = {}
    result_round_indexes: dict[str, dict[str, int]] = {}
    result_task_cards: dict[str, dict[str, str]] = {}
    seen_source_urls: dict[str, set[str]] = {}
    search_counts: dict[str, int] = {}
    specialist_loops: dict[tuple[str, str, str], SpecialistResearchLoop] = {}
    deep_task_state: dict[tuple[str, str], dict[str, object]] = {}
    _MAX_READS_PER_RESULT_ID = 1
    _MAX_READS_PER_SEARCH = 4
    findkg = LocalFinDKG(settings.findkg_data_dir)

    def directory(run_id: str) -> Path:
        path = settings.artifacts_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def deep_execution_key(context: ToolExecutionContext) -> tuple[str, str]:
        return context.run_id, context.agent_execution_id

    def execution_source_key(context: ToolExecutionContext) -> str:
        return f"{context.run_id}:{context.profile_id}:{context.agent_execution_id}"

    def search_lane_key(
        context: ToolExecutionContext, task_card_id: str | None
    ) -> str:
        execution_key = execution_source_key(context)
        return f"{execution_key}:{task_card_id}" if task_card_id else execution_key

    def publish_search_results(
        result: ResearchSearchResults,
        *,
        context: ToolExecutionContext,
        round_namespace: str,
        round_index: int,
        task_card_id: str | None,
        retrieval_notice: str | None,
    ) -> ResearchSearchResults:
        cache_key = execution_source_key(context)
        cache = source_cache.setdefault(cache_key, {})
        read_scopes = result_read_scope.setdefault(cache_key, {})
        round_indexes = result_round_indexes.setdefault(cache_key, {})
        task_cards = result_task_cards.setdefault(cache_key, {})
        seen = seen_source_urls.setdefault(
            search_lane_key(context, task_card_id), set()
        )
        novel: list[ResearchSource] = []
        duplicate_count = 0
        for source in result.items:
            canonical = canonical_source_url(source.url)
            if canonical and canonical in seen:
                duplicate_count += 1
                continue
            if canonical:
                seen.add(canonical)
            novel.append(source)
            if len(novel) >= settings.research_search_max_results:
                break

        public_items: list[ResearchSource] = []
        round_key = f"{cache_key}:{round_namespace}"
        read_guard.setdefault(round_key, {})
        for index, source in enumerate(novel, 1):
            public_id = f"src_{round_namespace}_{index:03d}"
            cache[public_id] = source.model_copy(deep=True)
            read_scopes[public_id] = round_key
            round_indexes[public_id] = round_index
            if task_card_id:
                task_cards[public_id] = task_card_id
            public_items.append(source.model_copy(update={
                "result_id": public_id,
                "summary": source.summary[:_MAX_SEARCH_SUMMARY_CHARS],
                "content": "",
            }))

        notices = [retrieval_notice] if retrieval_notice else []
        if duplicate_count:
            notices.append(f"已过滤 {duplicate_count} 个本 attempt 内重复来源")
        if result.items and not public_items:
            notices.append("本轮未发现新增来源；此前轮次结果仍可读取，请停止重复搜索并基于已有资料收束")
        return ResearchSearchResults(
            items=public_items,
            retrieval_notice="；".join(notices) or None,
        )

    def company_handler(_args: BaseModel, context: ToolExecutionContext):
        run = run_service.get_run(context.run_id)
        profile = get_company_profile(run.resolved_symbol or "", _as_date(run.as_of), settings)
        _atomic_model(profile, directory(run.run_id) / "company_profile.json")
        return profile.model_dump(mode="json")

    def specialist_loop(context: ToolExecutionContext) -> SpecialistResearchLoop | None:
        if context.profile_id not in {"business_research", "industry_research"}:
            return None
        key = (context.run_id, context.profile_id, context.agent_execution_id)
        if key in specialist_loops:
            return specialist_loops[key]
        run = run_service.get_run(context.run_id)
        plan_path = directory(context.run_id) / "lead_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
        loop = SpecialistResearchLoop(
            role="business" if context.profile_id == "business_research" else "industry",
            company_name=run.security_name or run.resolved_symbol or "当前公司",
            business_scope=list(plan.get("business_scope") or []),
            industry_scope=list(plan.get("industry_scope") or []),
        )
        specialist_loops[key] = loop
        return loop

    def persist_loop(context: ToolExecutionContext, loop: SpecialistResearchLoop) -> None:
        path = directory(context.run_id) / "research_loop.json"
        payload = {"loops": {}}
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("loops", {})[loop.role] = {
            **asdict(loop.audit), "stop_reason": loop.stop_reason,
        }
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def search_handler(args: BaseModel, context: ToolExecutionContext):
        assert isinstance(args, SearchInput)
        run = run_service.get_run(context.run_id)
        loop = specialist_loop(context)
        query = args.query
        cache_key = execution_source_key(context)
        deep_state_for_search: dict[str, object] | None = None
        if context.profile_id == "deep_research":
            review_path = directory(context.run_id) / "lead_review.json"
            review = json.loads(review_path.read_text(encoding="utf-8"))
            task_path = directory(context.run_id) / "deep_research_tasks.json"
            task_items = review.get("deep_research_tasks", [])
            if task_path.is_file():
                task_items = json.loads(task_path.read_text(encoding="utf-8")).get("tasks", task_items)
            task_ids = [item["task_id"] for item in task_items]
            if not args.task_card_id or args.task_card_id not in task_ids:
                raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Deep 检索必须指定有效 task_card_id")
            state = deep_task_state.setdefault(
                deep_execution_key(context),
                {
                    "active": task_ids[0] if task_ids else None,
                    "reads": set(),
                    "unresolved": set(),
                    "searches": {},
                    "tool_phase_started_at": _monotonic(),
                },
            )
            state.setdefault("unresolved", set())
            active = state["active"]
            if args.task_card_id != active:
                if active is not None and active not in state["reads"]:
                    state["unresolved"].add(active)
            searches = state["searches"]
            count = int(searches.get(args.task_card_id, 0))
            tool_phase_started_at = float(state["tool_phase_started_at"])
            if _monotonic() - tool_phase_started_at >= _DEEP_TOOL_PHASE_SECONDS:
                state["active"] = args.task_card_id
                state["unresolved"].add(args.task_card_id)
                return ResearchSearchResults(items=[], retrieval_notice=(
                    "Deep 工具阶段已为最终专题收束预留时间；请立即停止检索和读取，"
                    "基于已有 Evidence 输出全部任务卡结果，并将未覆盖项列为未解决"
                )).model_dump(mode="json")
            if count >= 2:
                # This is a normal completion boundary, not a retrieval
                # failure.  Let Deep close the card with the Evidence it has
                # already read instead of aborting the whole workflow.
                state["active"] = args.task_card_id
                state["unresolved"].add(args.task_card_id)
                return ResearchSearchResults(items=[], retrieval_notice=(
                    "本专题已达两轮检索上限；停止继续搜索，当前 attempt 前两轮结果仍可读取，"
                    "请基于已有 Evidence 输出并将未覆盖项列为未解决"
                )).model_dump(mode="json")
            deep_state_for_search = state
        if loop is not None:
            try:
                query = loop.start_query(args.query) if not loop.audit.rounds else loop.next_query([])
            except ValueError as exc:
                if loop.stop_reason == "max_rounds_reached":
                    persist_loop(context, loop)
                    return ResearchSearchResults(
                        items=[],
                        retrieval_notice=(
                            "已达两轮检索上限，请停止继续搜索；当前 attempt 前两轮结果仍可读取，"
                            "读取必要来源后立即基于已有 Evidence 输出研究简报并列明未解决项"
                        ),
                    ).model_dump(mode="json")
                raise ResearchSourceError(f"RESEARCH_SOURCE_FAILED: {exc}") from exc
        # Industry uses the generic Keenable provider directly. The configured
        # disclosure/AKShare providers are all ticker-bound and must not receive
        # an empty symbol for an external commodity-chain query.
        retrieval_notice: str | None = None
        fetch_limit = settings.research_search_max_results
        if seen_source_urls.get(search_lane_key(context, args.task_card_id)):
            fetch_limit = min(20, settings.research_search_max_results * 2)
        if (
            context.profile_id == "industry_research"
            and settings.research_search_mode == "live"
        ):
            from app.retrieval.registry import get_search_provider
            try:
                result = get_search_provider("keenable").search(
                    query=query, symbol="", max_results=fetch_limit,
                    timeout=settings.research_source_timeout,
                )
            except ResearchSourceError:
                # Industry Web retrieval supplements the research package. An
                # unavailable external provider must not prevent Lead/Writer
                # from delivering the best report supported by existing data.
                result = ResearchSearchResults(items=[])
                retrieval_notice = "行业外部检索暂不可用，基于已有资料继续研究"
        else:
            try:
                result = search_research_sources(
                    query,
                    run.resolved_symbol or "",
                    settings,
                    sources=args.sources or None,
                    max_results=fetch_limit,
                )
            except ResearchSourceError:
                if deep_state_for_search is None:
                    raise
                # A failed Deep source must neither consume the card's search
                # quota nor strand its state in "searched but unread". Mark it
                # unresolved so the model can continue with the next card or
                # close out using the evidence it already has.
                deep_state_for_search["active"] = args.task_card_id
                deep_state_for_search["unresolved"].add(args.task_card_id)
                return ResearchSearchResults(items=[], retrieval_notice=(
                    "本专题检索暂不可用，标记为未解决并基于已有资料继续"
                )).model_dump(mode="json")
        if deep_state_for_search is not None:
            deep_state_for_search["active"] = args.task_card_id
            deep_state_for_search["searches"][args.task_card_id] = count + 1
            deep_state_for_search["unresolved"].discard(args.task_card_id)
        search_counts[cache_key] = search_counts.get(cache_key, 0) + 1
        if context.profile_id == "deep_research":
            round_namespace = f"{args.task_card_id}_r{count + 1:02d}"
            round_index = count + 1
        elif loop is not None:
            round_namespace = f"r{len(loop.audit.rounds):02d}"
            round_index = len(loop.audit.rounds)
        else:
            round_namespace = f"r{search_counts[cache_key]:02d}"
            round_index = search_counts[cache_key]
        published = publish_search_results(
            result,
            context=context,
            round_namespace=round_namespace,
            round_index=round_index,
            task_card_id=args.task_card_id,
            retrieval_notice=retrieval_notice or result.retrieval_notice,
        )
        if loop is not None:
            loop.record_search(query, [item.url for item in published.items])
            persist_loop(context, loop)
        return published.model_dump(mode="json")

    def read_handler(args: BaseModel, context: ToolExecutionContext):
        assert isinstance(args, ReadSourceInput)
        loop = specialist_loop(context)
        cache_key = execution_source_key(context)
        if context.profile_id == "deep_research":
            state = deep_task_state.get(deep_execution_key(context), {})
            active = state.get("active")
            if not active:
                raise ResearchSourceError(
                    "RESEARCH_SOURCE_FAILED: Deep 尚未在当前 attempt 开始专题检索；"
                    "仅能读取当前 attempt 最新一轮搜索结果"
                )
            tool_phase_started_at = float(state["tool_phase_started_at"])
            if _monotonic() - tool_phase_started_at >= _DEEP_TOOL_PHASE_SECONDS:
                state["unresolved"].add(active)
                return ReadSourceOutput(
                    retrieval_notice=(
                        "Deep 工具阶段已为最终专题收束预留时间；请立即停止检索和读取，"
                        "基于已有 Evidence 输出全部任务卡结果，并将未覆盖项列为未解决"
                    )
                ).model_dump(mode="json")
        source = source_cache.get(cache_key, {}).get(args.result_id)
        if source is None:
            if context.profile_id == "deep_research":
                raise ValueError(
                    "搜索结果不存在；Deep 仅能读取当前 attempt 最新一轮搜索结果"
                )
            raise ValueError("搜索结果不存在或不属于当前任务")
        round_key = result_read_scope.get(cache_key, {}).get(args.result_id)
        if not round_key:
            raise ResearchSourceError(
                "RESEARCH_SOURCE_FAILED: 搜索结果缺少当前 attempt 的轮次信息"
            )
        guard = read_guard.setdefault(round_key, {})
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
        store = EvidenceStore(directory(context.run_id) / "evidence.json")
        item = store.find_by_url(source.url)
        if item is None:
            item = read_research_source(
                source,
                claim=args.claim,
                evidence_type=args.evidence_type,
                store=store,
                settings=settings,
            )
        guard[args.result_id] = already + 1
        if context.profile_id == "deep_research":
            state = deep_task_state[deep_execution_key(context)]
            task_card_id = result_task_cards.get(cache_key, {}).get(args.result_id)
            if task_card_id:
                state["reads"].add(task_card_id)
        if loop is not None:
            loop.record_read(
                item.id,
                args.claim,
                round_index=result_round_indexes[cache_key][args.result_id],
            )
            persist_loop(context, loop)
        return {"evidence_id": item.id, "source_name": item.source_name}

    def findkg_handler(args: BaseModel, context: ToolExecutionContext):
        assert isinstance(args, QueryFinDKGInput)
        run = run_service.get_run(context.run_id)
        if context.profile_id != "fundamental_lead" or run.status != "LEAD_PLANNING":
            from app.runtime.exceptions import ToolNotAllowedError

            raise ToolNotAllowedError("query_findkg 仅允许在 Lead Planning 阶段调用")
        return findkg.query(
            entities=args.entities,
            focus=args.focus,
            max_hops=args.max_hops,
            limit=args.limit,
        ).model_dump(mode="json")

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
        ToolDefinition("query_findkg", "Query FinDKG for financial, industry, commodity and macro entities related to the current research subject. Use only to discover relationships, variables and research directions during Lead Planning. Results are background hints, not Evidence.", QueryFinDKGInput, FinDKGQueryResult, {"full"}, {"fundamental_lead"}, settings.tool_default_timeout, "low", False, findkg_handler),
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
