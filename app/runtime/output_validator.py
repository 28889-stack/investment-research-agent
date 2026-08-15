from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.runtime.exceptions import AgentOutputError
from app.runtime.schemas import AgentNodeOutput
from app.fundamental.schemas import (
    FinalSynthesisOutput,
    FinancialResearchDraft,
    FundamentalWriterOutput,
    LeadSynthesisOutput,
    LeadFinalReviewOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    ValuationResearchOutput,
    WriterPlanOutput,
    WriterSectionOutput,
)
from app.technical.schemas import TechnicalAssemblyOutput, TechnicalResearchOutput
from app.charts.schemas import EvidenceChartExtractionOutput


TOP_LEVEL_FIELDS = set(AgentNodeOutput.model_fields)
CODE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
FORBIDDEN_RESEARCH_CONTENT = (
    re.compile(r"建议.{0,12}(买入|卖出|增持|减持)"),
    re.compile(r"(目标价|价格预测|投资结论)"),
    re.compile(r"(股价|市盈率|营收|净利润).{0,12}\d", re.IGNORECASE),
    re.compile(r"\b(buy|sell|target price|price forecast)\b", re.IGNORECASE),
)


class OutputValidator:
    def __init__(self, max_output_chars: int) -> None:
        self.max_output_chars = max_output_chars

    def validate(self, raw_output: str) -> AgentNodeOutput:
        return self.validate_for_schema(raw_output, "agent_node_output")

    def validate_for_schema(
        self, raw_output: str, schema_name: str
    ) -> AgentNodeOutput | TechnicalResearchOutput | TechnicalAssemblyOutput:
        if not raw_output or not raw_output.strip():
            raise AgentOutputError("OUTPUT_EMPTY", "Agent 输出为空")
        if len(raw_output) > self.max_output_chars:
            raise AgentOutputError("OUTPUT_TOO_LARGE", "Agent 输出超过长度限制")

        payload = self._extract_json(raw_output.strip())
        if not isinstance(payload, dict):
            raise AgentOutputError("SCHEMA_INVALID", "Agent 输出必须是 JSON 对象")
        model = output_model_for_schema(schema_name)
        allowed_fields = set(model.model_fields)
        unknown = set(payload) - allowed_fields
        if unknown:
            raise AgentOutputError(
                "FORBIDDEN_FIELD",
                f"Agent 输出包含禁止字段：{', '.join(sorted(unknown))}",
            )
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        if schema_name == "agent_node_output" and any(
            pattern.search(serialized_payload) for pattern in FORBIDDEN_RESEARCH_CONTENT
        ):
            raise AgentOutputError(
                "FORBIDDEN_CONTENT",
                "Runtime Smoke 输出不得包含行情数值、价格预测或投资结论",
            )
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise AgentOutputError("SCHEMA_INVALID", "Agent 输出不符合 Schema") from exc

    @staticmethod
    def _extract_json(raw_output: str):
        try:
            return json.loads(raw_output)
        except json.JSONDecodeError:
            pass

        fenced = CODE_BLOCK.search(raw_output)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError as exc:
                raise AgentOutputError("JSON_INVALID", "JSON 代码块格式无效") from exc

        if "{" not in raw_output:
            raise AgentOutputError("JSON_NOT_FOUND", "Agent 输出中未找到 JSON")

        decoder = json.JSONDecoder()
        saw_object_start = False
        for index, character in enumerate(raw_output):
            if character != "{":
                continue
            saw_object_start = True
            try:
                value, _end = decoder.raw_decode(raw_output[index:])
                return value
            except json.JSONDecodeError:
                continue
        if saw_object_start:
            raise AgentOutputError("JSON_INVALID", "Agent 输出中的 JSON 格式无效")
        raise AgentOutputError("JSON_NOT_FOUND", "Agent 输出中未找到 JSON")

def output_model_for_schema(schema_name: str):
    models = {
        "agent_node_output": AgentNodeOutput,
        "technical_research_output": TechnicalResearchOutput,
        "technical_assembly_output": TechnicalAssemblyOutput,
        "lead_plan_output": LeadPlanOutput,
        "specialist_research_output": SpecialistResearchOutput,
        "lead_review_output": LeadReviewOutput,
        "financial_research_draft": FinancialResearchDraft,
        "valuation_research_output": ValuationResearchOutput,
        "lead_final_review_output": LeadFinalReviewOutput,
        "fundamental_writer_output": FundamentalWriterOutput,
        "final_synthesis_output": FinalSynthesisOutput,
        "lead_synthesis_output": LeadSynthesisOutput,
        "writer_plan_output": WriterPlanOutput,
        "writer_section_output": WriterSectionOutput,
        "evidence_chart_extraction_output": EvidenceChartExtractionOutput,
    }
    try:
        return models[schema_name]
    except KeyError as exc:
        raise AgentOutputError("SCHEMA_INVALID", f"未知输出 Schema: {schema_name}") from exc
