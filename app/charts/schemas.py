from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ChartType = Literal[
    "line", "bar", "stacked_bar", "area", "candlestick", "combo",
    "band", "waterfall", "timeline",
]


class ChartSeries(StrictModel):
    series_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    values: list[float | None]
    style: Literal["line", "bar", "area", "open", "high", "low", "close"] = "line"
    axis: Literal["primary", "secondary"] = "primary"
    color: str = Field(default="#002FA7", pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("values")
    @classmethod
    def finite_values(cls, values: list[float | None]) -> list[float | None]:
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("图表数值必须有限或为空")
        return values


class ChartAnnotation(StrictModel):
    label: str = Field(min_length=1, max_length=160)
    index: int = Field(ge=0)
    value: float | None = None
    kind: Literal["event", "threshold", "cross", "breakout", "risk"] = "event"
    detail: str = Field(default="", max_length=500)


class ChartSpec(StrictModel):
    chart_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    section_id: str = Field(min_length=1, max_length=80)
    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{1,79}$")
    chart_type: ChartType
    title: str = Field(min_length=1, max_length=120)
    analytical_purpose: str = Field(default="", max_length=500)
    labels: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    unit: str = Field(default="", max_length=40)
    secondary_unit: str | None = Field(default=None, max_length=40)
    annotations: list[ChartAnnotation] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=2_000)
    rendering_notes: str = Field(default="", max_length=1_000)
    observation_points: list[str] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    data_lineage: dict[str, list[str]] = Field(default_factory=dict)
    placement: Literal["before_section", "after_claim", "after_body"] = "after_body"
    status: Literal["generated", "skipped"] = "generated"
    skip_reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_generated_shape(self) -> "ChartSpec":
        if self.status == "generated":
            if not self.labels or not self.series:
                raise ValueError("已生成图表必须包含标签和序列")
            if any(len(item.values) != len(self.labels) for item in self.series):
                raise ValueError("图表序列长度必须与标签一致")
            if self.skip_reason is not None:
                raise ValueError("已生成图表不得包含 skip_reason")
        elif not self.skip_reason:
            raise ValueError("跳过图表必须说明原因")
        return self


class ReportVisuals(StrictModel):
    version: str = "chart_spec_v1"
    charts: list[ChartSpec] = Field(default_factory=list)


class EvidenceChartPoint(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    evidence_id: str = Field(pattern=r"^ev_\d{3,}$")

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Evidence 图表数值必须有限")
        return value


class EvidenceChartCandidate(StrictModel):
    visual_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    series_name: str = Field(min_length=1, max_length=80)
    points: list[EvidenceChartPoint] = Field(min_length=1, max_length=80)


class EvidenceChartExtractionOutput(StrictModel):
    symbol: str
    as_of: str
    candidates: list[EvidenceChartCandidate] = Field(default_factory=list)
