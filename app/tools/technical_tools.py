from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.run_service import RunService
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import ToolExecutionContext
from app.runtime.tool_registry import ToolDefinition, ToolRegistry
from app.technical.indicators import (
    atomic_write_json,
    calculate_indicators,
    generate_technical_chart,
)
from app.technical.market_data import (
    atomic_write_csv,
    compute_data_version,
    get_market_data,
    load_persisted_market_data,
    resolve_security,
)
from app.technical.schemas import TechnicalIndicators
from app.technical.visuals import atomic_write_visuals, build_technical_visuals
from app.charts.schemas import ReportVisuals


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketDataSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    security_name: str
    as_of: str
    bar_count: int
    start_date: str
    end_date: str
    latest_close: float
    data_version: str


class TechnicalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    as_of: str
    data_version: str
    script_version: str
    latest_price: float
    trend: dict[str, Any]
    macd: dict[str, Any]
    rsi: dict[str, Any]
    kdj: dict[str, Any]
    bollinger: dict[str, Any]
    volatility: dict[str, Any]
    volume: dict[str, Any]
    support_resistance: dict[str, Any]
    patterns: list[str]
    signals: list[dict[str, Any]]
    chart_generated: bool | None = None
    chart_error: str | None = None
    visuals_generated: bool | None = None
    visuals_error: str | None = None


def _artifact_dir(settings: Settings, run_id: str) -> Path:
    path = settings.artifacts_dir / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summary(indicators: TechnicalIndicators, **extra: Any) -> dict[str, Any]:
    payload = indicators.model_dump(mode="json")
    payload.update(extra)
    return payload


def build_technical_tools(
    registry: ToolRegistry,
    service: RunService,
    repository: RuntimeRepository,
    settings: Settings,
) -> ToolRegistry:
    del repository  # ToolRegistry owns execution persistence.

    def market_data_tool(
        _arguments: BaseModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        run = service.get_run(context.run_id)
        resolved = resolve_security(run.input_symbol, settings)
        as_of = date.fromisoformat(run.as_of)
        frame = get_market_data(resolved.symbol, as_of, settings)
        path = _artifact_dir(settings, run.run_id) / "market_data.csv"
        atomic_write_csv(frame, path)
        data_version = compute_data_version(resolved.symbol, as_of, path)
        service.transition_run(
            run.run_id,
            status=run.status,
            stage=run.current_stage,
            progress=run.progress,
            event_type="MARKET_DATA_SAVED",
            message="标准化行情已保存",
            normalized_symbol=resolved.symbol,
            resolved_symbol=resolved.symbol,
            security_name=resolved.security_name,
            data_version=data_version,
            current_node=run.current_node,
            event_key=f"{run.run_id}:market_data:saved:{data_version}",
        )
        return {
            "symbol": resolved.symbol,
            "security_name": resolved.security_name,
            "as_of": run.as_of,
            "bar_count": len(frame),
            "start_date": frame["date"].iloc[0].date().isoformat(),
            "end_date": frame["date"].iloc[-1].date().isoformat(),
            "latest_close": float(frame["close"].iloc[-1]),
            "data_version": data_version,
        }

    def indicator_tool(
        _arguments: BaseModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        run = service.get_run(context.run_id)
        if not run.resolved_symbol or not run.data_version:
            raise ValueError("MARKET_DATA_FAILED: 必须先调用 get_market_data")
        directory = _artifact_dir(settings, run.run_id)
        frame = load_persisted_market_data(
            directory / "market_data.csv",
            symbol=run.resolved_symbol,
            as_of=date.fromisoformat(run.as_of),
            expected_data_version=run.data_version,
            min_bars=settings.market_data_min_bars,
        )
        indicators, enriched = calculate_indicators(
            frame,
            symbol=run.resolved_symbol,
            as_of=date.fromisoformat(run.as_of),
            data_version=run.data_version,
            script_version=settings.technical_indicator_version,
        )
        atomic_write_json(indicators, directory / "technical_indicators.json")
        visuals_path = directory / "technical_visuals.json"
        visuals_path.unlink(missing_ok=True)
        chart_path = directory / "technical_chart.png"
        chart_path.unlink(missing_ok=True)
        generate_technical_chart(enriched, indicators, chart_path)
        atomic_write_visuals(
            build_technical_visuals(enriched, indicators),
            visuals_path,
        )
        return _summary(
            indicators,
            chart_generated=True,
            chart_error=None,
            visuals_generated=True,
            visuals_error=None,
        )

    def summary_tool(
        _arguments: BaseModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        run = service.get_run(context.run_id)
        path = _artifact_dir(settings, run.run_id) / "technical_indicators.json"
        indicators = TechnicalIndicators.model_validate_json(path.read_text(encoding="utf-8"))
        if indicators.data_version != run.data_version:
            raise ValueError("MARKET_DATA_INVALID: 指标数据版本不一致")
        visuals_path = path.with_name("technical_visuals.json")
        visuals_generated = False
        visuals_error = None
        if visuals_path.is_file():
            try:
                ReportVisuals.model_validate_json(visuals_path.read_text(encoding="utf-8"))
                visuals_generated = True
            except (OSError, ValueError):
                visuals_error = "technical_visuals.json 无法校验"
        return _summary(
            indicators,
            chart_generated=(path.with_name("technical_chart.png")).is_file(),
            chart_error=None,
            visuals_generated=visuals_generated,
            visuals_error=visuals_error,
        )

    common = {
        "allowed_modes": {"full"},
        "supported_profiles": {settings.technical_research_profile},
        "timeout_seconds": settings.tool_default_timeout,
        "cost_level": "low",
    }
    registry.register(
        ToolDefinition(
            name="get_market_data",
            description="解析当前任务证券并获取、校验、保存标准化日线行情，只返回摘要",
            input_model=EmptyInput,
            output_model=MarketDataSummary,
            side_effect=True,
            handler=market_data_tool,
            **common,
        )
    )
    registry.register(
        ToolDefinition(
            name="calculate_technical_indicators",
            description="读取当前任务行情，计算技术指标，生成必备行情全景图及实际识别形态的原生解释图",
            input_model=EmptyInput,
            output_model=TechnicalSummary,
            side_effect=True,
            handler=indicator_tool,
            **common,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_technical_summary",
            description="读取当前任务已校验的技术指标精简摘要",
            input_model=EmptyInput,
            output_model=TechnicalSummary,
            side_effect=False,
            handler=summary_tool,
            **common,
        )
    )
    return registry
