from __future__ import annotations

from datetime import date
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolvedSecurity(StrictModel):
    ticker: str
    exchange: Literal["SH", "SZ", "BJ"]
    symbol: str
    security_name: str


class TrendIndicators(StrictModel):
    sma5: float
    sma20: float
    sma60: float
    alignment: str


class MACDIndicators(StrictModel):
    dif: float
    dea: float
    histogram: float
    cross: str


class RSIIndicators(StrictModel):
    rsi14: float
    state: str


class KDJIndicators(StrictModel):
    k: float
    d: float
    j: float
    cross: str


class BollingerIndicators(StrictModel):
    upper: float
    middle: float
    lower: float


class VolatilityIndicators(StrictModel):
    atr14: float
    annualized_volatility_20: float


class VolumeIndicators(StrictModel):
    latest: float
    ma5: float
    ma20: float


class SupportResistance(StrictModel):
    support_20: float
    support_60: float
    resistance_20: float
    resistance_60: float


ChartFamily = Literal["price_trend", "macd", "rsi", "volume_price"]


class PatternSignal(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    detected_at: date
    chart_family: ChartFamily
    trigger_values: dict[str, float] = Field(default_factory=dict)
    trigger_rule: str = Field(min_length=1, max_length=300)
    confirmation_rule: str = Field(min_length=1, max_length=300)
    invalidation_rule: str = Field(min_length=1, max_length=300)

    @field_validator("trigger_values")
    @classmethod
    def finite_trigger_values(cls, values: dict[str, float]) -> dict[str, float]:
        if not values or not all(math.isfinite(value) for value in values.values()):
            raise ValueError("形态触发值必须为非空有限数值")
        return values


class TechnicalIndicators(StrictModel):
    symbol: str
    as_of: date
    data_version: str
    script_version: str
    latest_price: float
    trend: TrendIndicators
    macd: MACDIndicators
    rsi: RSIIndicators
    kdj: KDJIndicators
    bollinger: BollingerIndicators
    volatility: VolatilityIndicators
    volume: VolumeIndicators
    support_resistance: SupportResistance
    patterns: list[str]
    signals: list[PatternSignal] = Field(default_factory=list)


class TechnicalResearchOutput(StrictModel):
    symbol: str
    as_of: date
    data_version: str
    trend: str
    volume_price: str
    momentum: str
    volatility: str
    support_resistance: str
    patterns: list[str]
    short_term: str
    medium_term: str
    long_term: str
    conflicts: list[str]
    risks: list[str]
    confidence: Literal["low", "medium", "high"]


class DirectionProbability(StrictModel):
    up: float = Field(ge=0, le=1)
    flat: float = Field(ge=0, le=1)
    down: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> "DirectionProbability":
        values = (self.up, self.flat, self.down)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("direction probabilities must be finite")
        if abs(sum(values) - 1) > 1e-6:
            raise ValueError("direction probabilities must sum to 1")
        return self


class KronosResult(StrictModel):
    symbol: str
    as_of: date
    horizon: str
    direction_probability: DirectionProbability
    expected_return_range: tuple[float, float]
    model_confidence: float = Field(ge=0, le=1)
    model_version: str
    data_version: str

    @model_validator(mode="after")
    def valid_range(self) -> "KronosResult":
        if not all(math.isfinite(value) for value in self.expected_return_range):
            raise ValueError("expected return range must be finite")
        if self.expected_return_range[0] > self.expected_return_range[1]:
            raise ValueError("invalid expected return range")
        return self


class TechnicalAssemblyOutput(StrictModel):
    symbol: str
    as_of: date
    data_version: str
    summary: str
    agreements: list[str]
    conflicts: list[str]
    uncertainties: list[str]
    short_term: str
    medium_term: str
    long_term: str
    risks: list[str]
    conclusion: str
    disclaimer: str
