from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.fundamental.workflow import FundamentalWorkflow
from app.fundamental.schemas import DeepResearchQuery, DeepResearchTaskCard
from app.run_service import RunService
from app.runtime.exceptions import AgentTimeoutError
from app.runtime.pi_client import MockPiClient
from app.runtime.repository import RuntimeRepository


NODES = [
    "resolve_security",
    "lead_planning",
    "business_research",
    "industry_research",
    "lead_review",
    "deep_research",
    "assemble_retrieval_package",
    "financial_research",
    "valuation_research",
    "lead_final_review",
    "lead_synthesis",
    "writer_planning",
    "build_fundamental_visuals",
    "fundamental_writer",
    "final_synthesis",
    "write_fundamental_report",
]

ARTIFACTS = {
    "company_profile.json",
    "financial_data.json",
    "financial_metrics.json",
    "evidence.json",
    "assumptions.json",
    "lead_plan.json",
    "business_research.json",
    "industry_research.json",
    "lead_review.json",
    "deep_research_tasks.json",
    "deep_research.json",
    "financial_research.json",
    "valuation_result.json",
    "valuation_research.json",
    "lead_final_review.json",
    "retrieval_package.json",
    "research_loop.json",
    "lead_synthesis.json",
    "writer_plan.json",
    "fundamental_chart_candidates.json",
    "fundamental_research_package.md",
    "writer_section_business.json",
    "writer_section_industry.json",
    "writer_section_financial.json",
    "final_synthesis.json",
    "fundamental_writer.json",
    "fundamental_report.md",
    "report_visuals.json",
    "fundamental_report.html",
    "result_manifest.json",
}


def _service(settings, session_factory):
    return RunService(
        session_factory,
        settings.artifacts_dir,
        settings.pi_runtime_mode,
        settings.technical_workflow_version,
        settings.fundamental_workflow_version,
    )


def _run(settings, session_factory):
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    return service, run.run_id


def _workflow(settings, session_factory, interrupt_after=None):
    return FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=MockPiClient(),
        interrupt_after=interrupt_after,
    )


def test_deep_retrieval_runs_cards_in_parallel(settings, session_factory, monkeypatch) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    calls: list[tuple[str, str]] = []

    def execute(name, arguments, context, profile):
        calls.append((name, arguments.get("task_card_id", "")))
        time.sleep(0.03)
        if name == "search_research_sources":
            return {"items": [{"result_id": f"{arguments['task_card_id']}_src", "title": "t"}]}
        return {"evidence_id": f"ev_{arguments['result_id']}"}

    monkeypatch.setattr(workflow.tool_registry, "execute", execute)
    cards = [
        DeepResearchTaskCard(
            task_id=f"deep_{index:02d}",
            topic=f"专题{index}",
            scope="scope",
            research_questions=[f"问题{index}"],
        )
        for index in range(1, 4)
    ]
    queries = [
        DeepResearchQuery(task_id=card.task_id, queries=[f"检索{card.task_id}"])
        for card in cards
    ]
    started = time.monotonic()
    audit = workflow._parallel_deep_retrieval(run_id, cards, queries, "parallel-test")
    elapsed = time.monotonic() - started
    workflow.shutdown()

    assert elapsed < 0.16
    assert all(audit[card.task_id]["search_count"] == 1 for card in cards)
    assert all(audit[card.task_id]["read_count"] == 1 for card in cards)
    assert {task_id for name, task_id in calls if name == "search_research_sources"} == {
        card.task_id for card in cards
    }


class InvalidLeadEvidenceClient(MockPiClient):
    def run_agent(self, **kwargs):
        raw = super().run_agent(**kwargs)
        session = self.sessions[kwargs["session_id"]]
        if session["profile"]["profile_id"] == "fundamental_lead" and kwargs["context"].get("node") == "lead_planning":
            payload = json.loads(raw)
            payload["evidence_ids"] = ["ev_999"]
            return json.dumps(payload, ensure_ascii=False)
        return raw


class LeadWithoutSourceReadClient(MockPiClient):
    def run_agent(self, **kwargs):
        session = self.sessions[kwargs["session_id"]]
        context = kwargs["context"]
        if (
            session["profile"]["profile_id"] == "fundamental_lead"
            and context.get("node") == "lead_planning"
        ):
            kwargs["tool_handler"]("get_company_profile", {})
            kwargs["tool_handler"](
                "search_research_sources", {"query": "公司经营与行业暴露"}
            )
            run = context["run"]
            return json.dumps(
                {
                    "symbol": run["resolved_symbol"],
                    "as_of": run["as_of"],
                    "thesis": "先形成公司经营与外部定价变量的问题地图。",
                    "key_questions": ["外部变量如何传导至公司盈利"],
                    "business_scope": ["公司产销量、售价与成本如何变化"],
                    "industry_scope": ["行业供需与宏观定价变量如何变化"],
                    "industry_types": ["资源采掘与商品定价"],
                    "financial_focus": ["收入、利润与现金流传导"],
                    "valuation_focus": ["估值对盈利和价格假设的敏感性"],
                    "risks_to_verify": ["商品价格与成本波动"],
                    "evidence_ids": [],
                },
                ensure_ascii=False,
            )
        return super().run_agent(**kwargs)


class ResearchAgentsWithoutSourceReadClient(MockPiClient):
    def run_agent(self, **kwargs):
        session = self.sessions[kwargs["session_id"]]
        profile_id = session["profile"]["profile_id"]
        context = kwargs["context"]
        run = context.get("run", {})
        symbol = run.get("resolved_symbol", "")
        if profile_id == "business_research":
            kwargs["tool_handler"]("get_company_profile", {})
            kwargs["tool_handler"](
                "search_research_sources", {"query": "公司经营与项目进展"}
            )
            return json.dumps(
                {
                    "symbol": symbol,
                    "summary": "已形成公司经营研究框架，当前搜索结果尚不足以形成可引用事实。",
                    "findings": [],
                    "risks": [],
                    "missing_information": ["待补充可读取的公司经营来源"],
                    "topics": [],
                },
                ensure_ascii=False,
            )
        if profile_id == "industry_research":
            kwargs["tool_handler"](
                "search_research_sources", {"query": "行业供需与宏观定价"}
            )
            return json.dumps(
                {
                    "symbol": symbol,
                    "summary": "已形成行业研究框架，当前搜索结果尚不足以形成可引用事实。",
                    "findings": [],
                    "risks": [],
                    "missing_information": ["待补充可读取的行业来源"],
                    "topics": [],
                },
                ensure_ascii=False,
            )
        if profile_id == "deep_research":
            cards = context["artifacts"]["lead_review"].get(
                "deep_research_tasks", []
            )
            for card in cards:
                kwargs["tool_handler"](
                    "search_research_sources",
                    {"query": card["topic"], "task_card_id": card["task_id"]},
                )
            return json.dumps(
                {
                    "symbol": symbol,
                    "summary": "已执行专题搜索，但没有可读取来源，保留为未解决事项。",
                    "findings": [],
                    "risks": [],
                    "missing_information": ["专题来源暂不可读取"],
                    "topics": [
                        {
                            "task_id": card["task_id"],
                            "topic": card["topic"],
                            "summary": "当前未形成可靠增量结论。",
                            "findings": [],
                            "risks": [],
                            "missing_information": card["research_questions"],
                        }
                        for card in cards
                    ],
                },
                ensure_ascii=False,
            )
        return super().run_agent(**kwargs)


class SpecialistTimeoutThenSynthesisClient(MockPiClient):
    def __init__(
        self,
        target_profile: str,
        service: RunService | None = None,
        *,
        cancel_timing: str | None = None,
    ) -> None:
        super().__init__()
        self.target_profile = target_profile
        self.service = service
        self.cancel_timing = cancel_timing
        self.target_calls = 0
        self.retry_tools: list[dict] | None = None
        self.retry_has_evidence = False
        self.first_evidence_id = ""

    def run_agent(self, **kwargs):
        session = self.sessions[kwargs["session_id"]]
        if session["profile"]["profile_id"] != self.target_profile:
            return super().run_agent(**kwargs)

        self.target_calls += 1
        context = kwargs["context"]
        tool_handler = kwargs["tool_handler"]
        symbol = context["run"]["resolved_symbol"]
        if self.target_calls == 1:
            if self.target_profile == "business_research":
                tool_handler("get_company_profile", {})
            sources = tool_handler(
                "search_research_sources",
                {
                    "query": (
                        "公司业务研究"
                        if self.target_profile == "business_research"
                        else "行业供需与定价"
                    )
                },
            )
            if self.target_profile == "business_research":
                selected = sources["items"][-1]
                evidence = tool_handler(
                    "read_research_source",
                    {
                        "result_id": selected["result_id"],
                        "claim": "第一次 attempt 已取得的研究证据",
                        "evidence_type": "historical_fact",
                    },
                )
                self.first_evidence_id = evidence["evidence_id"]
            else:
                # Lead Planning has already produced ev_001. Industry's first
                # attempt proves retrieval completion; the synthesis retry may
                # use any bounded current-run Evidence supplied by workflow.
                self.first_evidence_id = "ev_001"
            if self.cancel_timing in {"before_retry", "after_return"}:
                assert self.service is not None
                self.service.request_cancel(context["run"]["run_id"])
            if self.cancel_timing != "after_return":
                raise AgentTimeoutError("specialist timed out after retrieval")
        else:
            self.retry_tools = list(session["tools"])
            self.retry_has_evidence = bool(
                context.get("artifacts", {}).get("evidence", {}).get("items")
            )

        return json.dumps(
            {
                "symbol": symbol,
                "summary": "基于第一次 attempt 已获得的 Evidence 完成收束。",
                "findings": [
                    {
                        "claim": "已有证据支持当前的方向性研究结论。",
                        "evidence_ids": [self.first_evidence_id],
                        "confidence": "medium",
                    }
                ],
                "risks": [],
                "missing_information": ["未覆盖的事项保留为未解决项"],
            },
            ensure_ascii=False,
        )


class DeepTimeoutThenSynthesisClient(MockPiClient):
    def __init__(self) -> None:
        super().__init__()
        self.deep_calls = 0
        self.retry_tools: list[dict] | None = None
        self.retry_has_evidence = False

    def run_agent(self, **kwargs):
        session = self.sessions[kwargs["session_id"]]
        if session["profile"]["profile_id"] != "deep_research":
            return super().run_agent(**kwargs)

        self.deep_calls += 1
        context = kwargs["context"]
        cards = context["artifacts"]["lead_review"]["deep_research_tasks"]
        if self.deep_calls == 1:
            for card in cards:
                kwargs["tool_handler"](
                    "search_research_sources",
                    {
                        "query": card["topic"],
                        "task_card_id": card["task_id"],
                    },
                )
            raise AgentTimeoutError("deep timed out after retrieval")

        self.retry_tools = list(session["tools"])
        self.retry_has_evidence = bool(
            context.get("artifacts", {}).get("evidence", {}).get("items")
        )
        symbol = context["run"]["resolved_symbol"]
        return json.dumps(
            {
                "symbol": symbol,
                "summary": "基于第一次 attempt 的检索与 Evidence 完成专题收束。",
                "findings": [],
                "risks": [],
                "missing_information": ["没有可靠增量的事项保留为未解决项"],
                "topics": [
                    {
                        "task_id": card["task_id"],
                        "topic": card["topic"],
                        "summary": "已完成专题收束。",
                        "findings": [],
                        "risks": [],
                        "missing_information": card["research_questions"],
                    }
                    for card in cards
                ],
            },
            ensure_ascii=False,
        )


def test_fundamental_graph_has_extended_research_and_writer_planning_nodes(settings, session_factory) -> None:
    workflow = _workflow(settings, session_factory)
    try:
        nodes = set(workflow.graph.get_graph().nodes) - {"__start__", "__end__"}
    finally:
        workflow.shutdown()
    assert nodes == set(NODES)


def test_lead_planning_can_finish_without_reading_a_source(
    settings, session_factory
) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=LeadWithoutSourceReadClient(),
        interrupt_after=["lead_planning"],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    execution = next(
        item
        for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "lead_planning"
    )
    tools = RuntimeRepository(session_factory).list_tool_executions(
        execution.execution_id
    )
    plan = json.loads(
        (settings.artifacts_dir / run_id / "lead_plan.json").read_text(
            encoding="utf-8"
        )
    )

    assert state["error_message"] is None
    assert execution.status == "COMPLETED"
    assert [item.tool_name for item in tools] == [
        "get_company_profile",
        "search_research_sources",
    ]
    assert plan["evidence_ids"] == []


def test_research_agents_can_return_unresolved_results_without_source_reads(
    settings, session_factory
) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=ResearchAgentsWithoutSourceReadClient(),
        interrupt_after=["deep_research"],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    executions = {
        item.node_name: item
        for item in RuntimeRepository(session_factory).list_executions(run_id)
    }
    for node in ("business_research", "industry_research", "deep_research"):
        execution = executions[node]
        tools = RuntimeRepository(session_factory).list_tool_executions(
            execution.execution_id
        )
        assert execution.status == "COMPLETED"
        assert "read_research_source" not in {item.tool_name for item in tools}
    assert state["error_message"] is None


def test_specialist_runtime_tasks_keep_prior_round_results_readable(
    settings, session_factory
) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory, interrupt_after=["industry_research"])
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    contexts = {
        item.node_name: json.loads(item.input_context_json)
        for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name in {"business_research", "industry_research"}
    }
    for node in ("business_research", "industry_research"):
        task = contexts[node]["task"]
        assert "停止继续搜索" in task
        assert "前两轮" in task
        assert "停止调用工具" not in task


@pytest.mark.parametrize(
    "target_profile", ["business_research", "industry_research"]
)
def test_specialist_timeout_after_retrieval_retries_as_evidence_only_synthesis(
    settings, session_factory, target_profile
) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    client = SpecialistTimeoutThenSynthesisClient(target_profile)
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=client,
        interrupt_after=[target_profile],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    directory = settings.artifacts_dir / run_id
    executions = [
        item
        for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == target_profile
    ]
    assert state["error_message"] is None
    assert (directory / f"{target_profile}.json").is_file()
    assert [(item.attempt, item.status) for item in executions] == [
        (1, "FAILED"),
        (2, "COMPLETED"),
    ]
    assert client.target_calls == 2
    assert client.retry_tools == []
    assert client.retry_has_evidence is True


def test_deep_timeout_after_retrieval_retries_as_evidence_only_synthesis(
    settings, session_factory
) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    client = DeepTimeoutThenSynthesisClient()
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=client,
        interrupt_after=["deep_research"],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    executions = [
        item
        for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "deep_research"
    ]
    assert state["error_message"] is None
    assert [(item.attempt, item.status) for item in executions] == [
        (1, "FAILED"),
        (2, "COMPLETED"),
    ]
    assert client.deep_calls == 2
    assert client.retry_tools == []
    assert client.retry_has_evidence is True
    assert (settings.artifacts_dir / run_id / "deep_research.json").is_file()


def test_cancel_requested_between_specialist_attempts_prevents_retry(
    settings, session_factory
) -> None:
    service, run_id = _run(settings, session_factory)
    client = SpecialistTimeoutThenSynthesisClient(
        "industry_research", service, cancel_timing="before_retry"
    )
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=client,
        interrupt_after=["industry_research"],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    assert state["error_message"] == "CANCELLED"
    assert service.get_run(run_id).status == "CANCELLED"
    assert client.target_calls == 1
    assert not (settings.artifacts_dir / run_id / "industry_research.json").exists()


def test_cancel_requested_after_specialist_returns_skips_semantic_commit(
    settings, session_factory
) -> None:
    service, run_id = _run(settings, session_factory)
    client = SpecialistTimeoutThenSynthesisClient(
        "business_research", service, cancel_timing="after_return"
    )
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=client,
        interrupt_after=["business_research"],
    )
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    assert state["error_message"] == "CANCELLED"
    assert service.get_run(run_id).status == "CANCELLED"
    assert client.target_calls == 1
    assert not (settings.artifacts_dir / run_id / "business_research.json").exists()


def test_fundamental_graph_adds_retrieval_synthesis_and_writer_planning_nodes(
    settings, session_factory
) -> None:
    workflow = _workflow(settings, session_factory)
    try:
        nodes = set(workflow.graph.get_graph().nodes) - {"__start__", "__end__"}
    finally:
        workflow.shutdown()

    assert {
        "assemble_retrieval_package",
        "lead_synthesis",
        "writer_planning",
    } <= nodes


def test_fundamental_mock_workflow_generates_complete_research_package(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    run = service.get_run(run_id)
    directory = settings.artifacts_dir / run_id
    assert run.status == "COMPLETED"
    assert run.workflow_name == "fundamental_v1"
    assert run.resolved_symbol == "600519.SH"
    assert ARTIFACTS == {item.name for item in directory.iterdir() if not item.name.startswith(".")}
    assert state["report_path"] == str(directory / "fundamental_report.html")
    package = (directory / "fundamental_research_package.md").read_text(encoding="utf-8")
    report = (directory / "fundamental_report.md").read_text(encoding="utf-8")
    metrics = json.loads((directory / "financial_metrics.json").read_text(encoding="utf-8"))
    valuation = json.loads((directory / "valuation_result.json").read_text(encoding="utf-8"))
    assert "本文档是第四阶段生成的基本面研究工作包" in package
    assert "# 个股基本面分析报告" in report
    assert str(metrics["cash_flow"][metrics["periods"][-1]]["free_cash_flow"]) in report
    assert str(valuation["relative"]["pe"]["value"]) in report
    assert "正式报告将在 Fundamental Writer 阶段生成" in package
    assert "本工作包不构成投资建议、交易指令或收益承诺。" in package
    visuals = json.loads((directory / "report_visuals.json").read_text(encoding="utf-8"))
    assert len([item for item in visuals["charts"] if item["status"] == "generated"]) >= 3
    assert {item["plugin_id"] for item in visuals["charts"]} >= {
        "financial_performance_trend", "profitability_quality", "cashflow_capex"
    }


def test_completed_fundamental_run_uses_html_report_as_the_final_artifact(
    settings, session_factory
) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    run = service.get_run(run_id)
    assert state["report_path"].endswith("fundamental_report.html")
    assert run.report_path and run.report_path.endswith("fundamental_report.html")
    assert (settings.artifacts_dir / run_id / "fundamental_report.html").is_file()


def test_agent_execution_permissions_and_assumption_handoff(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()
    executions = RuntimeRepository(session_factory).list_executions(run_id)
    completed = {item.node_name: item for item in executions if item.status == "COMPLETED"}

    assert set(completed) == {
        "lead_planning", "business_research", "industry_research", "lead_review",
        "deep_research",
        "lead_synthesis", "writer_planning",
        "chart_data_extractor",
        "financial_research", "valuation_research", "lead_final_review",
        "writer_section_business", "writer_section_industry", "writer_section_financial",
        "final_synthesis",
    }
    assert completed["lead_planning"].tool_call_count == 3
    assert completed["business_research"].tool_call_count == 3
    assert completed["industry_research"].tool_call_count == 2
    assert completed["lead_review"].tool_call_count == 0
    assert completed["deep_research"].tool_call_count == 2
    assert completed["financial_research"].tool_call_count == 0
    assert completed["valuation_research"].tool_call_count == 0
    assert completed["lead_final_review"].tool_call_count == 0
    assert completed["lead_synthesis"].tool_call_count == 0
    assert completed["writer_planning"].tool_call_count == 0
    assert completed["chart_data_extractor"].tool_call_count == 0
    assert completed["writer_section_business"].tool_call_count == 0
    assert completed["writer_section_industry"].tool_call_count == 0
    assert completed["writer_section_financial"].tool_call_count == 0
    assert completed["final_synthesis"].tool_call_count == 0
    directory = settings.artifacts_dir / run_id
    assumptions = json.loads((directory / "assumptions.json").read_text())["items"]
    valuation = json.loads((directory / "valuation_research.json").read_text())
    assert [item["id"] for item in assumptions] == ["asm_001", "asm_002", "asm_003"]
    assert valuation["assumption_ids"] == ["asm_001", "asm_002", "asm_003"]


def test_lead_review_tasks_are_passed_to_deep_research(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    execution = next(
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "deep_research"
    )
    context = json.loads(execution.input_context_json)
    assert context["node"] == "deep_research"
    assert "lead_review" in context["artifacts"]
    assert context["artifacts"]["lead_review"]["financial_questions"]
    assert context["artifacts"]["lead_review"]["deep_research_tasks"]
    assert "deep_research_tasks" in context["task"]
    tasks = json.loads((settings.artifacts_dir / run_id / "deep_research_tasks.json").read_text())
    output = json.loads((settings.artifacts_dir / run_id / "deep_research.json").read_text())
    assert tasks["tasks"] and output["topics"][0]["task_id"] == tasks["tasks"][0]["task_id"]


def test_fundamental_report_api_exposes_lightweight_package_metadata(settings, session_factory, client) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_count"] == 2
    assert payload["assumption_count"] == 3
    assert payload["ready_for_writer"] is True
    assert payload["writer_status"] == "completed"
    assert payload["report_status"] == "current"
    assert payload["result_version"] == 1
    assert payload["missing_information"] == []


def test_resume_does_not_repeat_completed_agent(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["business_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    repository = RuntimeRepository(session_factory)
    assert len(repository.list_executions(run_id)) == 2

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()
    executions = repository.list_executions(run_id)
    assert len([item for item in executions if item.node_name == "lead_planning"]) == 1
    assert len([item for item in executions if item.node_name == "business_research"]) == 1


def test_corrupt_artifact_rebuilds_from_corresponding_node(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["industry_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "business_research.json"
    path.write_text("broken", encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    assert json.loads(path.read_text())["symbol"] == "600519.SH"
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "business_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_valid_but_tampered_financial_metrics_are_recomputed_on_resume(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["financial_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "financial_metrics.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    latest = payload["periods"][-1]
    expected = payload["cash_flow"][latest]["free_cash_flow"]
    payload["cash_flow"][latest]["free_cash_flow"] = expected + 123.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["cash_flow"][latest]["free_cash_flow"] == expected
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "financial_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_valid_but_tampered_valuation_result_is_recomputed_on_resume(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["valuation_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "valuation_result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload["relative"]["pe"]["value"]
    payload["relative"]["pe"]["value"] = expected + 1.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["relative"]["pe"]["value"] == expected
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "valuation_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_cancelled_fundamental_run_stops_before_first_node(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    service.transition_run(run_id, status="RESOLVING_SECURITY", stage="解析证券", progress=1, message="test")
    service.request_cancel(run_id)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    assert service.get_run(run_id).status == "CANCELLED"
    assert state["error_message"] == "CANCELLED"


def test_semantically_invalid_agent_output_is_not_kept_completed(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = FundamentalWorkflow(settings, session_factory, pi_client=InvalidLeadEvidenceClient())
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    execution = RuntimeRepository(session_factory).list_executions(run_id)[0]
    assert service.get_run(run_id).status == "FAILED"
    assert state["error_message"] == "LEAD_AGENT_FAILED"
    assert execution.status == "FAILED"
    assert execution.error_type == "SEMANTIC_VALIDATION_FAILED"
