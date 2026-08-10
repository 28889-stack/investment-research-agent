from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AnalysisType = Literal["technical", "fundamental"]


class RunCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    analysis_type: AnalysisType
    policy_id: str = Field(default="general_research", min_length=1, max_length=64)
    as_of: date | None = None

    @field_validator("symbol", "policy_id")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class RunCreated(BaseModel):
    run_id: str
    status: str


class RunEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_type: str
    stage: str
    message: str
    payload_json: str | None
    created_at: str


class UsageSummary(BaseModel):
    agent_calls: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    cost_currency: str | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    input_symbol: str
    normalized_symbol: str | None
    resolved_symbol: str | None
    security_name: str | None
    data_version: str | None
    analysis_type: str
    policy_id: str
    as_of: str
    status: str
    current_stage: str
    progress: int
    cancel_requested: bool
    error_message: str | None
    report_ready: bool = False
    created_at: str
    updated_at: str
    current_node: str | None
    runtime_mode: str | None
    checkpoint_enabled: bool
    writer_status: str | None = None
    report_status: str | None = None
    ready_for_writer: bool | None = None
    result_version: int | None = None
    stale_results: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    usage: UsageSummary = Field(default_factory=UsageSummary)


class RunDetail(RunSummary):
    started_at: str | None
    completed_at: str | None
    events: list[RunEventResponse]


class CancelResponse(BaseModel):
    run_id: str
    status: str
    cancel_requested: bool


class ReportResponse(BaseModel):
    run_id: str
    input_symbol: str
    normalized_symbol: str | None
    analysis_type: str
    policy_id: str
    as_of: str
    markdown: str
    html: str
    resolved_symbol: str | None = None
    security_name: str | None = None
    data_version: str | None = None
    indicator_version: str | None = None
    kronos_model_version: str | None = None
    chart_url: str | None = None
    evidence_count: int | None = None
    assumption_count: int | None = None
    ready_for_writer: bool | None = None
    missing_information: list[str] = Field(default_factory=list)
    writer_status: str | None = None
    report_status: str | None = None
    result_version: int | None = None
    stale_results: list[str] = Field(default_factory=list)


class RuntimeHealth(BaseModel):
    runtime_mode: str
    bridge_status: str
    profiles_loaded: int
    tools_registered: int
    checkpoint_status: str


class AgentExecutionSummary(BaseModel):
    execution_id: str
    node_name: str
    profile_id: str
    profile_version: str
    status: str
    tool_call_count: int
    started_at: str
    completed_at: str | None
    error_type: str | None
    error_message: str | None
    validated_summary: str | None
