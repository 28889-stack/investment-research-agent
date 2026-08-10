from __future__ import annotations

import json
from typing import Any

from app.run_service import RunService
from app.runtime.exceptions import ContextTooLargeError
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import AgentNodeOutput, AgentProfile
from app.runtime.tool_registry import ToolRegistry
from app.runtime.output_validator import output_model_for_schema
from app.technical.schemas import KronosResult, TechnicalIndicators, TechnicalResearchOutput
from app.fundamental.result_manifest import ResultManifestStore, sha256_file
from app.fundamental.schemas import (
    AssumptionStore,
    CompanyProfile,
    EvidenceCollection,
    FinancialData,
    FinancialMetrics,
    FinancialResearchOutput,
    LeadFinalReviewOutput,
    LeadSynthesisOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    ValuationResearchOutput,
    ValuationResult,
    RetrievalPackage,
    WriterPlanOutput,
)


class ContextLoader:
    def __init__(
        self,
        run_service: RunService,
        repository: RuntimeRepository,
        *,
        max_context_chars: int,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.run_service = run_service
        self.repository = repository
        self.max_context_chars = max_context_chars
        self.tool_registry = tool_registry

    def load_for_agent(
        self,
        run_id: str,
        profile: AgentProfile,
        node_name: str,
        context_refs: list[str],
        task: str,
        *,
        output_schema_name: str | None = None,
    ) -> dict[str, Any]:
        schema_name = output_schema_name or profile.output_schema
        run = self.run_service.get_run(run_id)
        run_context = {
            "run_id": run.run_id,
            "input_symbol": run.input_symbol,
            "analysis_type": run.analysis_type,
            "as_of": run.as_of,
        }
        if schema_name != "agent_node_output":
            run_context.update(
                {
                    "resolved_symbol": run.resolved_symbol,
                    "security_name": run.security_name,
                    "data_version": run.data_version,
                }
            )
        if schema_name == "lead_final_review_output":
            context = self._load_fundamental_final_review_context(
                run, node_name, context_refs, task
            )
        elif profile.mode == "full":
            allowed_tools: list[Any]
            if self.tool_registry is None:
                allowed_tools = list(profile.allowed_tools)
            else:
                allowed_tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                    }
                    for tool in self.tool_registry.list_for_profile(profile)
                ]
            context: dict[str, Any] = {
                "run": run_context,
                "node": node_name,
                "task": task,
                "allowed_tools": allowed_tools,
                "output_schema": output_model_for_schema(
                    schema_name
                ).model_json_schema(),
            }
            if context_refs:
                context["artifacts"] = self._load_fundamental_artifacts(run, context_refs)
        elif schema_name == "technical_assembly_output":
            context = self._load_technical_assembly_context(
                run, node_name, context_refs, task
            )
        elif schema_name in {"financial_research_draft", "valuation_research_output"}:
            context = {
                "run": run_context,
                "node": node_name,
                "task": task,
                "artifacts": self._load_fundamental_artifacts(run, context_refs),
                "output_schema": output_model_for_schema(schema_name).model_json_schema(),
            }
        elif schema_name == "lead_synthesis_output":
            context = self._load_lead_synthesis_context(run, node_name, context_refs, task)
        elif schema_name == "writer_plan_output":
            context = self._load_writer_plan_context(run, node_name, context_refs, task)
        elif schema_name == "fundamental_writer_output":
            context = self._load_fundamental_writer_context(
                run, node_name, context_refs, task
            )
        else:
            upstream = self._load_validated_upstream(run_id, context_refs)
            context = {
                "run": run_context,
                "node": node_name,
                "task": task,
                "upstream": upstream,
                "output_schema": output_model_for_schema(
                    profile.output_schema
                ).model_json_schema(),
            }

        if len(json.dumps(context, ensure_ascii=False)) > self.max_context_chars:
            raise ContextTooLargeError(
                f"受控上下文超过上限 {self.max_context_chars} 字符"
            )
        return context

    def _load_fundamental_final_review_context(
        self, run, node_name: str, context_refs: list[str], task: str
    ) -> dict[str, Any]:
        required = {
            "artifact:lead_plan", "artifact:business_research", "artifact:industry_research",
            "artifact:lead_review", "artifact:deep_research", "artifact:financial_data",
            "artifact:financial_metrics", "artifact:financial_research", "artifact:valuation_research",
            "artifact:retrieval_package", "artifact:assumptions",
        }
        if set(context_refs) != required or len(context_refs) != len(required):
            raise ValueError("Lead Final Review Artifact 引用不在专用白名单")
        structured_refs = [
            reference for reference in context_refs
            if reference not in {"artifact:financial_data", "artifact:financial_metrics"}
        ]
        artifacts = self._load_fundamental_artifacts(
            run, structured_refs, evidence_excerpt_chars=150
        )
        directory = self.run_service.artifacts_dir / run.run_id
        data = FinancialData.model_validate_json(
            (directory / "financial_data.json").read_text(encoding="utf-8")
        )
        metrics = FinancialMetrics.model_validate_json(
            (directory / "financial_metrics.json").read_text(encoding="utf-8")
        )
        latest = data.periods[-1]
        latest_period = latest.period
        artifacts["financial_data_summary"] = {
            "symbol": data.symbol,
            "as_of": data.as_of.isoformat(),
            "latest_period": latest.model_dump(mode="json"),
        }
        artifacts["financial_metrics_summary"] = {
            "symbol": metrics.symbol,
            "as_of": metrics.as_of.isoformat(),
            "latest_period": latest_period,
            "growth": metrics.growth.get(latest_period, {}),
            "profitability": metrics.profitability.get(latest_period, {}),
            "balance_sheet": metrics.balance_sheet.get(latest_period, {}),
            "cash_flow": metrics.cash_flow.get(latest_period, {}),
        }
        return {
            "run": {
                "run_id": run.run_id,
                "resolved_symbol": run.resolved_symbol,
                "security_name": run.security_name,
                "as_of": run.as_of,
            },
            "node": node_name,
            "task": task,
            "allowed_tools": [],
            "artifacts": artifacts,
            "output_schema": output_model_for_schema("lead_final_review_output").model_json_schema(),
        }

    def _load_lead_synthesis_context(
        self, run, node_name: str, context_refs: list[str], task: str
    ) -> dict[str, Any]:
        required = {
            "artifact:lead_plan", "artifact:business_research", "artifact:industry_research",
            "artifact:lead_review", "artifact:deep_research", "artifact:financial_research",
            "artifact:valuation_research", "artifact:lead_final_review", "artifact:retrieval_package",
            "artifact:assumptions",
        }
        if set(context_refs) != required or len(context_refs) != len(required):
            raise ValueError("Lead Synthesis Artifact 引用不在专用白名单")
        return {
            "run": {"run_id": run.run_id, "resolved_symbol": run.resolved_symbol, "security_name": run.security_name, "as_of": run.as_of},
            "node": node_name,
            "task": task,
            "allowed_tools": [],
            "artifacts": self._load_fundamental_artifacts(run, context_refs),
            "output_schema": output_model_for_schema("lead_synthesis_output").model_json_schema(),
        }

    def _load_writer_plan_context(
        self, run, node_name: str, context_refs: list[str], task: str
    ) -> dict[str, Any]:
        required = {
            "artifact:lead_synthesis", "artifact:lead_final_review", "artifact:business_research",
            "artifact:industry_research", "artifact:deep_research", "artifact:financial_research",
            "artifact:valuation_research", "artifact:financial_metrics", "artifact:valuation_result",
        }
        if set(context_refs) != required or len(context_refs) != len(required):
            raise ValueError("Writer Plan Artifact 引用不在专用白名单")
        return {
            "run": {"run_id": run.run_id, "resolved_symbol": run.resolved_symbol, "security_name": run.security_name, "as_of": run.as_of},
            "node": node_name,
            "task": task,
            "artifacts": self._load_fundamental_artifacts(run, context_refs),
            "output_schema": output_model_for_schema("writer_plan_output").model_json_schema(),
        }

    def _load_fundamental_writer_context(
        self, run, node_name: str, context_refs: list[str], task: str
    ) -> dict[str, Any]:
        required = {
            "artifact:lead_synthesis",
            "artifact:writer_plan",
            "artifact:business_research",
            "artifact:industry_research",
            "artifact:deep_research",
            "artifact:financial_research",
            "artifact:valuation_research",
            "artifact:lead_final_review",
            "artifact:retrieval_package",
            "artifact:assumptions",
            "artifact:company_profile",
            "artifact:financial_metrics",
            "artifact:valuation_result",
        }
        if set(context_refs) != required or len(context_refs) != len(required):
            raise ValueError("Fundamental Writer Artifact 引用不在专用白名单")
        directory = self.run_service.artifacts_dir / run.run_id
        store = ResultManifestStore(
            directory, run.run_id, self.run_service.fundamental_workflow_version
        )
        manifest = store.load()
        allowed_names = {reference.partition(":")[2] for reference in required}
        stale = set(store.audit(persist=False))
        for name in allowed_names:
            entry = manifest.results.get(name)
            path = directory / f"{name}.json"
            if (
                entry is None
                or entry.status != "current"
                or name in stale
                or not path.is_file()
                or sha256_file(path) != entry.sha256
            ):
                raise ValueError(f"Fundamental Writer 输入不是 current: {name}")
        structured_names = (
            "lead_synthesis", "writer_plan", "business_research", "industry_research",
            "deep_research", "financial_research", "valuation_research", "lead_final_review",
            "retrieval_package", "assumptions",
        )
        artifacts = self._load_fundamental_artifacts(
            run, [f"artifact:{name}" for name in structured_names]
        )
        company_model = CompanyProfile.model_validate_json(
            (directory / "company_profile.json").read_text(encoding="utf-8")
        )
        metrics_model = FinancialMetrics.model_validate_json(
            (directory / "financial_metrics.json").read_text(encoding="utf-8")
        )
        valuation_model = ValuationResult.model_validate_json(
            (directory / "valuation_result.json").read_text(encoding="utf-8")
        )
        if any(
            item.symbol != run.resolved_symbol
            for item in (company_model, metrics_model, valuation_model)
        ):
            raise ValueError("Fundamental Writer 安全摘要身份不一致")
        if any(
            item.as_of.isoformat() != run.as_of
            for item in (company_model, metrics_model, valuation_model)
        ):
            raise ValueError("Fundamental Writer 安全摘要 as_of 不一致")
        company = company_model.model_dump(mode="json")
        metrics = metrics_model.model_dump(mode="json")
        valuation = valuation_model.model_dump(mode="json")
        periods = metrics.get("periods") or []
        latest = periods[-1] if periods else None
        artifacts["company_profile_summary"] = {
            key: company.get(key)
            for key in ("symbol", "company_name", "short_name", "industry", "business_summary", "currency", "as_of", "data_source")
        }
        artifacts["financial_metrics_summary"] = {
            "symbol": metrics.get("symbol"),
            "as_of": metrics.get("as_of"),
            "script_version": metrics.get("script_version"),
            "latest_period": latest,
            "latest": {
                group: (metrics.get(group) or {}).get(latest, {})
                for group in ("growth", "profitability", "balance_sheet", "cash_flow", "efficiency")
            } if latest else {},
            "missing_metrics": metrics.get("missing_metrics", []),
        }
        artifacts["valuation_result_summary"] = {
            "symbol": valuation.get("symbol"),
            "as_of": valuation.get("as_of"),
            "script_version": valuation.get("script_version"),
            "relative": valuation.get("relative"),
            "dcf": valuation.get("dcf"),
            "assumption_ids": valuation.get("assumption_ids", []),
        }
        return {
            "run": {
                "run_id": run.run_id,
                "resolved_symbol": run.resolved_symbol,
                "security_name": run.security_name,
                "as_of": run.as_of,
            },
            "node": node_name,
            "task": task,
            "artifacts": artifacts,
            "output_schema": output_model_for_schema("fundamental_writer_output").model_json_schema(),
        }

    def _load_fundamental_artifacts(
        self, run, context_refs: list[str], *, evidence_excerpt_chars: int = 1_000
    ) -> dict[str, Any]:
        allowed = {
            "company_profile",
            "evidence",
            "assumptions",
            "lead_plan",
            "business_research",
            "industry_research",
            "deep_research",
            "lead_review",
            "financial_data",
            "financial_metrics",
            "financial_research",
            "valuation_result",
            "valuation_research",
            "lead_final_review",
            "retrieval_package",
            "lead_synthesis",
            "writer_plan",
        }
        names: list[str] = []
        for reference in context_refs:
            prefix, separator, name = reference.partition(":")
            if separator != ":" or prefix != "artifact" or name not in allowed:
                raise ValueError("基本面 Agent 引用了非白名单 Artifact")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("基本面 Artifact 引用不得重复")
        directory = self.run_service.artifacts_dir / run.run_id
        result: dict[str, Any] = {}
        models = {
            "company_profile": CompanyProfile,
            "evidence": EvidenceCollection,
            "assumptions": AssumptionStore,
            "lead_plan": LeadPlanOutput,
            "business_research": SpecialistResearchOutput,
            "industry_research": SpecialistResearchOutput,
            "deep_research": SpecialistResearchOutput,
            "lead_review": LeadReviewOutput,
            "financial_data": FinancialData,
            "financial_metrics": FinancialMetrics,
            "financial_research": FinancialResearchOutput,
            "valuation_result": ValuationResult,
            "valuation_research": ValuationResearchOutput,
            "lead_final_review": LeadFinalReviewOutput,
            "retrieval_package": RetrievalPackage,
            "lead_synthesis": LeadSynthesisOutput,
            "writer_plan": WriterPlanOutput,
        }
        for name in names:
            payload = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
            payload = models[name].model_validate(payload).model_dump(mode="json")
            if isinstance(payload, dict) and "symbol" in payload and payload["symbol"] != run.resolved_symbol:
                raise ValueError("基本面 Artifact symbol 与当前任务不一致")
            if isinstance(payload, dict) and payload.get("as_of") is not None and str(payload["as_of"]) != run.as_of:
                raise ValueError("基本面 Artifact as_of 与当前任务不一致")
            if name == "evidence":
                payload = _bounded_evidence_context(
                    payload, excerpt_chars=evidence_excerpt_chars
                )
            result[name] = payload
        return result

    def _load_technical_assembly_context(
        self, run, node_name: str, context_refs: list[str], task: str
    ) -> dict[str, Any]:
        required = {
            "artifact:technical_research",
            "artifact:kronos_result",
            "artifact:technical_indicators",
        }
        if set(context_refs) != required or len(context_refs) != len(required):
            raise ValueError("Technical Assembly 只能读取三个已校验产物引用")
        directory = self.run_service.artifacts_dir / run.run_id
        research = TechnicalResearchOutput.model_validate_json(
            (directory / "technical_research.json").read_text(encoding="utf-8")
        )
        kronos = KronosResult.model_validate_json(
            (directory / "kronos_result.json").read_text(encoding="utf-8")
        )
        indicators = TechnicalIndicators.model_validate_json(
            (directory / "technical_indicators.json").read_text(encoding="utf-8")
        )
        identities = {
            (research.symbol, research.data_version),
            (kronos.symbol, kronos.data_version),
            (indicators.symbol, indicators.data_version),
        }
        if identities != {(run.resolved_symbol, run.data_version)}:
            raise ValueError("Technical Assembly 上下文身份或数据版本不一致")
        indicator_summary = indicators.model_dump(mode="json")
        return {
            "run": {
                "run_id": run.run_id,
                "symbol": run.resolved_symbol,
                "security_name": run.security_name,
                "as_of": run.as_of,
                "data_version": run.data_version,
            },
            "node": node_name,
            "task": task,
            "technical_research": research.model_dump(mode="json"),
            "kronos": kronos.model_dump(mode="json"),
            "indicators": indicator_summary,
            "output_schema": output_model_for_schema(
                "technical_assembly_output"
            ).model_json_schema(),
        }

    def _load_validated_upstream(
        self, run_id: str, context_refs: list[str]
    ) -> dict[str, Any]:
        if len(context_refs) != 1 or not context_refs[0].startswith("execution:"):
            raise ValueError("Constrained Agent 需要一个已校验 execution 引用")
        execution_id = context_refs[0].partition(":")[2]
        execution = self.repository.get_execution(execution_id)
        if execution.run_id != run_id:
            raise ValueError("上游执行不属于当前任务")
        if execution.status != "COMPLETED" or not execution.validated_output_json:
            raise ValueError("上游执行尚未产生已校验输出")
        output = AgentNodeOutput.model_validate_json(execution.validated_output_json)
        return {
            "summary": output.summary,
            "findings": [finding.model_dump(mode="json") for finding in output.findings],
        }


def _bounded_evidence_context(payload: Any, *, excerpt_chars: int = 1_000) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Evidence Artifact 结构不正确")
    items: list[dict[str, Any]] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            raise ValueError("Evidence item 结构不正确")
        item = {key: value for key, value in raw.items() if key != "content"}
        item["content_excerpt"] = str(raw.get("content") or "")[:excerpt_chars]
        item["content_is_untrusted"] = True
        items.append(item)
    return {"items": items}
