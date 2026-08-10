from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.fundamental.schemas import (
    FinancialResearchDraft,
    FundamentalWriterOutput,
    LeadSynthesisOutput,
    LeadFinalReviewOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    ValuationResearchOutput,
    WriterPlanOutput,
)
from app.technical.schemas import TechnicalAssemblyOutput, TechnicalResearchOutput


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    version: str = Field(min_length=1, max_length=32)
    role: str = Field(min_length=1, max_length=80)
    mode: Literal["full", "constrained"]
    system_prompt: str = Field(min_length=1, max_length=8_000)
    allowed_tools: list[str]
    max_iterations: int = Field(ge=1, le=12)
    max_tool_calls: int = Field(ge=0, le=12)
    context_policy: str = Field(min_length=1, max_length=80)
    output_schema: Literal[
        "agent_node_output",
        "technical_research_output",
        "technical_assembly_output",
        "lead_plan_output",
        "specialist_research_output",
        "lead_review_output",
        "financial_research_draft",
        "valuation_research_output",
        "lead_final_review_output",
        "fundamental_writer_output",
        "lead_synthesis_output",
        "writer_plan_output",
    ]
    model: str | None = None
    timeout_seconds: float = Field(gt=0, le=300)

    @field_validator("allowed_tools")
    @classmethod
    def unique_tools(cls, tools: list[str]) -> list[str]:
        if len(tools) != len(set(tools)):
            raise ValueError("allowed_tools 不能重复")
        return tools

    @model_validator(mode="after")
    def enforce_constrained_limits(self) -> "AgentProfile":
        if self.mode == "constrained":
            if self.max_iterations != 1 or self.max_tool_calls != 0:
                raise ValueError("Constrained Profile 只允许一次迭代且禁止工具调用")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=2_000)
    evidence_ids: list[str]
    assumption_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class AgentNodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=120)
    status: Literal["completed", "incomplete"]
    summary: str = Field(min_length=1, max_length=4_000)
    findings: list[Finding]
    new_evidence: list[dict]
    new_assumptions: list[dict]
    risks: list[str]
    conflicts: list[str]
    missing_information: list[str]
    suggested_followups: list[str]

    @field_validator("new_evidence", "new_assumptions")
    @classmethod
    def phase_two_keeps_compatibility_arrays_empty(cls, values: list[dict]) -> list[dict]:
        if values:
            raise ValueError("第二阶段不创建 Evidence 或 Assumption")
        return values


class AgentExecutionResult(BaseModel):
    execution_id: str
    session_id: str
    node_name: str
    profile_id: str
    profile_version: str
    tool_call_count: int
    output: (
        AgentNodeOutput
        | TechnicalResearchOutput
        | TechnicalAssemblyOutput
        | LeadPlanOutput
        | SpecialistResearchOutput
        | LeadReviewOutput
        | FinancialResearchDraft
        | ValuationResearchOutput
        | LeadFinalReviewOutput
        | FundamentalWriterOutput
        | LeadSynthesisOutput
        | WriterPlanOutput
    )
    reused: bool = False


class ToolExecutionContext(BaseModel):
    run_id: str
    agent_execution_id: str
    profile_id: str
    profile_mode: Literal["full", "constrained"]
