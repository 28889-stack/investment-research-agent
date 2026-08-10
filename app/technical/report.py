from __future__ import annotations

import os
import re
from pathlib import Path

from app.models import ResearchRun
from app.technical.market_data import load_persisted_market_data
from app.technical.schemas import (
    KronosResult,
    TechnicalAssemblyOutput,
    TechnicalIndicators,
    TechnicalResearchOutput,
)


DISCLAIMER = """本报告仅基于历史行情、技术指标和模型预测生成。
技术指标及模型预测不能保证未来表现。
本报告不构成投资建议、交易指令或收益承诺。"""


class ReportError(RuntimeError):
    code = "REPORT_GENERATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def _items(values: list[str]) -> str:
    return (
        "\n".join(f"- {_safe_narrative(value)}" for value in values)
        if values
        else "- 无明确项目"
    )


def _authoritative_items(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "- 无明确项目"


# Agent prose is qualitative only. Any Arabic numeral makes the whole field
# ineligible for the deterministic report; authoritative code-generated
# numbers and pattern names use separate render paths.
EXACT_NUMBER = re.compile(r"\d")


def _safe_narrative(value: str) -> str:
    if EXACT_NUMBER.search(value):
        return "Agent 原叙述含精确数值，已省略；请以本报告确定性数值为准。"
    return value


def generate_technical_report(run: ResearchRun, artifact_dir: Path) -> Path:
    try:
        indicators = TechnicalIndicators.model_validate_json(
            (artifact_dir / "technical_indicators.json").read_text(encoding="utf-8")
        )
        research = TechnicalResearchOutput.model_validate_json(
            (artifact_dir / "technical_research.json").read_text(encoding="utf-8")
        )
        kronos = KronosResult.model_validate_json(
            (artifact_dir / "kronos_result.json").read_text(encoding="utf-8")
        )
        assembly = TechnicalAssemblyOutput.model_validate_json(
            (artifact_dir / "technical_assembly.json").read_text(encoding="utf-8")
        )
        identities = {
            (item.symbol, item.data_version)
            for item in (indicators, research, kronos, assembly)
        }
        if identities != {(run.resolved_symbol, run.data_version)}:
            raise ReportError("报告输入的证券或数据版本不一致")
        market = load_persisted_market_data(
            artifact_dir / "market_data.csv",
            symbol=run.resolved_symbol or "",
            as_of=indicators.as_of,
            expected_data_version=run.data_version or "",
        )
        probability = kronos.direction_probability
        expected_range = kronos.expected_return_range
        levels = indicators.support_resistance
        chart = artifact_dir / "technical_chart.png"
        chart_text = (
            f"![技术面图表](/api/runs/{run.run_id}/artifacts/technical_chart.png)"
            if chart.is_file()
            else "技术图表生成失败，本报告其余结果不受影响。"
        )
        report = f"""# 个股技术面分析报告

## 一、证券与数据说明

- 股票名称：{run.security_name}
- 标准证券代码：{run.resolved_symbol}
- 数据截止日期：{run.as_of}
- 日线数量：{len(market)}
- data_version：`{run.data_version}`

{chart_text}

## 二、趋势分析

- 最新收盘价：{indicators.latest_price}
- SMA5：{indicators.trend.sma5}
- SMA20：{indicators.trend.sma20}
- SMA60：{indicators.trend.sma60}
- 均线排列：{indicators.trend.alignment}

{_safe_narrative(research.trend)}

## 三、量价关系

- 最新成交量：{indicators.volume.latest}
- Volume MA5：{indicators.volume.ma5}
- Volume MA20：{indicators.volume.ma20}

{_safe_narrative(research.volume_price)}

## 四、动量指标

- MACD DIF：{indicators.macd.dif}
- MACD DEA：{indicators.macd.dea}
- MACD 柱：{indicators.macd.histogram}
- MACD 状态：{indicators.macd.cross}
- RSI14：{indicators.rsi.rsi14}（{indicators.rsi.state}）
- KDJ：K {indicators.kdj.k} / D {indicators.kdj.d} / J {indicators.kdj.j}（{indicators.kdj.cross}）
- 布林带：上轨 {indicators.bollinger.upper} / 中轨 {indicators.bollinger.middle} / 下轨 {indicators.bollinger.lower}

{_safe_narrative(research.momentum)}

## 五、波动率

- ATR14：{indicators.volatility.atr14}
- 20 日年化历史波动率：{indicators.volatility.annualized_volatility_20:.2%}

{_safe_narrative(research.volatility)}

## 六、支撑位与阻力位

- 20 日支撑位：{levels.support_20}
- 60 日支撑位：{levels.support_60}
- 20 日阻力位：{levels.resistance_20}
- 60 日阻力位：{levels.resistance_60}

{_safe_narrative(research.support_resistance)}

## 七、技术形态候选

{_authoritative_items(indicators.patterns)}

## 八、Kronos 模型结果

- 预测周期：{kronos.horizon}
- 上涨概率：{probability.up:.2%}
- 震荡概率：{probability.flat:.2%}
- 下跌概率：{probability.down:.2%}
- 预期收益区间：{expected_range[0]:.2%} ～ {expected_range[1]:.2%}
- 模型置信度：{kronos.model_confidence:.2%}

## 九、信号一致与冲突

Assembly 摘要：{_safe_narrative(assembly.summary)}

一致信号：

{_items(assembly.agreements)}

冲突信号：

{_items(assembly.conflicts)}

不确定性：

{_items(assembly.uncertainties)}

## 十、短期、中期和长期观察

- 短期：{_safe_narrative(assembly.short_term)}
- 中期：{_safe_narrative(assembly.medium_term)}
- 长期：{_safe_narrative(assembly.long_term)}

结论：{_safe_narrative(assembly.conclusion)}

## 十一、风险与限制

Technical Research 风险：

{_items(research.risks)}

Assembly 风险：

{_items(assembly.risks)}

## 十二、版本信息

- 技术指标脚本版本：`{indicators.script_version}`
- Kronos 模型版本：`{kronos.model_version}`
- 技术工作流版本：`{run.workflow_name}`
- 数据版本：`{indicators.data_version}`

## 免责声明

{DISCLAIMER}
"""
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / "technical_report.md"
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(report, encoding="utf-8")
        os.replace(temporary, path)
        return path
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError("技术面报告生成失败") from exc


def technical_report_is_current(run: ResearchRun, artifact_dir: Path) -> bool:
    path = artifact_dir / "technical_report.md"
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        indicators = TechnicalIndicators.model_validate_json(
            (artifact_dir / "technical_indicators.json").read_text(encoding="utf-8")
        )
        research = TechnicalResearchOutput.model_validate_json(
            (artifact_dir / "technical_research.json").read_text(encoding="utf-8")
        )
        kronos = KronosResult.model_validate_json(
            (artifact_dir / "kronos_result.json").read_text(encoding="utf-8")
        )
        assembly = TechnicalAssemblyOutput.model_validate_json(
            (artifact_dir / "technical_assembly.json").read_text(encoding="utf-8")
        )
        identities = {
            (item.symbol, item.data_version)
            for item in (indicators, research, kronos, assembly)
        }
        if identities != {(run.resolved_symbol, run.data_version)}:
            return False
        market = load_persisted_market_data(
            artifact_dir / "market_data.csv",
            symbol=run.resolved_symbol or "",
            as_of=indicators.as_of,
            expected_data_version=run.data_version or "",
        )
        probability = kronos.direction_probability
        levels = indicators.support_resistance
        chart_marker = (
            f"/api/runs/{run.run_id}/artifacts/technical_chart.png"
            if (artifact_dir / "technical_chart.png").is_file()
            else "技术图表生成失败"
        )
        required = [
            "# 个股技术面分析报告",
            str(run.resolved_symbol),
            str(run.data_version),
            f"日线数量：{len(market)}",
            indicators.script_version,
            kronos.model_version,
            f"上涨概率：{probability.up:.2%}",
            f"震荡概率：{probability.flat:.2%}",
            f"下跌概率：{probability.down:.2%}",
            f"20 日支撑位：{levels.support_20}",
            f"60 日阻力位：{levels.resistance_60}",
            chart_marker,
            DISCLAIMER,
            _safe_narrative(research.trend),
            _safe_narrative(research.volume_price),
            _safe_narrative(research.momentum),
            _safe_narrative(research.volatility),
            _safe_narrative(research.support_resistance),
            _safe_narrative(assembly.summary),
            _safe_narrative(assembly.short_term),
            _safe_narrative(assembly.medium_term),
            _safe_narrative(assembly.long_term),
            _safe_narrative(assembly.conclusion),
        ]
        # Pattern names are deterministic, code-generated facts and may
        # legitimately contain digits (for example, "20日突破").  Only
        # free-form agent prose goes through the precise-number guard.
        required.extend(indicators.patterns)
        for values in (
            research.risks,
            assembly.agreements,
            assembly.conflicts,
            assembly.uncertainties,
            assembly.risks,
        ):
            required.extend(_safe_narrative(value) for value in values)
        return all(value in text for value in required)
    except (OSError, ValueError, ReportError):
        return False
