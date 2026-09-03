from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.technical.schemas import PatternSignal, TechnicalIndicators


class IndicatorError(ValueError):
    code = "INDICATOR_CALCULATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def _number(value: float) -> float:
    if not np.isfinite(value):
        raise IndicatorError("指标结果包含非有限数值")
    return round(float(value), 6)


def _cross_state(current_a: float, current_b: float, previous_a: float, previous_b: float) -> str:
    if current_a >= current_b and previous_a < previous_b:
        return "golden_cross"
    if current_a <= current_b and previous_a > previous_b:
        return "death_cross"
    return "bullish" if current_a >= current_b else "bearish"


def _signal(
    name: str,
    detected_at: date,
    chart_family: str,
    trigger_values: dict[str, float],
    trigger_rule: str,
    confirmation_rule: str,
    invalidation_rule: str,
) -> PatternSignal:
    return PatternSignal.model_validate(
        {
            "name": name,
            "detected_at": detected_at,
            "chart_family": chart_family,
            "trigger_values": {
                key: _number(value) for key, value in trigger_values.items()
            },
            "trigger_rule": trigger_rule,
            "confirmation_rule": confirmation_rule,
            "invalidation_rule": invalidation_rule,
        }
    )


def calculate_indicators(
    market_data: pd.DataFrame,
    *,
    symbol: str,
    as_of: date,
    data_version: str,
    script_version: str,
) -> tuple[TechnicalIndicators, pd.DataFrame]:
    try:
        frame = market_data.copy()
        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        volume = frame["volume"].astype(float)

        for window in (5, 20, 60):
            frame[f"sma{window}"] = close.rolling(window).mean()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        frame["macd_dif"] = ema12 - ema26
        frame["macd_dea"] = frame["macd_dif"].ewm(span=9, adjust=False).mean()
        frame["macd_histogram"] = (frame["macd_dif"] - frame["macd_dea"]) * 2

        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi = rsi.where(loss != 0, 100)
        rsi = rsi.where(~((gain == 0) & (loss == 0)), 50)
        frame["rsi14"] = rsi.fillna(50)

        lowest9 = low.rolling(9).min()
        highest9 = high.rolling(9).max()
        rsv = ((close - lowest9) / (highest9 - lowest9).replace(0, np.nan) * 100).fillna(50)
        frame["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        frame["kdj_d"] = frame["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
        frame["kdj_j"] = 3 * frame["kdj_k"] - 2 * frame["kdj_d"]

        frame["boll_middle"] = close.rolling(20).mean()
        boll_std = close.rolling(20).std(ddof=0)
        frame["boll_upper"] = frame["boll_middle"] + 2 * boll_std
        frame["boll_lower"] = frame["boll_middle"] - 2 * boll_std

        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        frame["atr14"] = true_range.rolling(14).mean()
        frame["annualized_volatility_20"] = close.pct_change().rolling(20).std(ddof=0) * np.sqrt(252)
        frame["volume_ma5"] = volume.rolling(5).mean()
        frame["volume_ma20"] = volume.rolling(20).mean()

        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        alignment = "mixed"
        if latest["sma5"] > latest["sma20"] > latest["sma60"]:
            alignment = "bullish"
        elif latest["sma5"] < latest["sma20"] < latest["sma60"]:
            alignment = "bearish"
        macd_cross = _cross_state(
            latest["macd_dif"], latest["macd_dea"], previous["macd_dif"], previous["macd_dea"]
        )
        kdj_cross = _cross_state(
            latest["kdj_k"], latest["kdj_d"], previous["kdj_k"], previous["kdj_d"]
        )
        rsi_state = "overbought" if latest["rsi14"] >= 70 else "oversold" if latest["rsi14"] <= 30 else "neutral"

        detected_at = pd.Timestamp(latest["date"]).date()
        signals: list[PatternSignal] = []
        prior20 = frame.iloc[-21:-1]
        prior20_high = float(prior20["high"].max())
        prior20_low = float(prior20["low"].min())
        if latest["close"] > prior20_high:
            signals.append(
                _signal(
                    "20日突破",
                    detected_at,
                    "price_trend",
                    {"latest_close": latest["close"], "prior_20_high": prior20_high},
                    "收盘价高于前20个交易日最高价",
                    "后续收盘价维持在突破位上方，且量能不明显衰减",
                    "收盘价重新跌回原突破位下方",
                )
            )
        if latest["close"] < prior20_low:
            signals.append(
                _signal(
                    "20日跌破",
                    detected_at,
                    "price_trend",
                    {"latest_close": latest["close"], "prior_20_low": prior20_low},
                    "收盘价低于前20个交易日最低价",
                    "后续收盘价继续位于跌破位下方",
                    "收盘价重新站回原跌破位上方",
                )
            )
        if alignment == "bullish":
            signals.append(
                _signal(
                    "均线多头排列",
                    detected_at,
                    "price_trend",
                    {"sma5": latest["sma5"], "sma20": latest["sma20"], "sma60": latest["sma60"]},
                    "SMA5 高于 SMA20，且 SMA20 高于 SMA60",
                    "均线顺序延续，价格保持在中短期均线上方",
                    "SMA5 下穿 SMA20 或均线顺序被破坏",
                )
            )
        elif alignment == "bearish":
            signals.append(
                _signal(
                    "均线空头排列",
                    detected_at,
                    "price_trend",
                    {"sma5": latest["sma5"], "sma20": latest["sma20"], "sma60": latest["sma60"]},
                    "SMA5 低于 SMA20，且 SMA20 低于 SMA60",
                    "均线顺序延续，价格保持在中短期均线下方",
                    "SMA5 上穿 SMA20 或均线顺序被破坏",
                )
            )
        if macd_cross == "golden_cross":
            signals.append(
                _signal(
                    "MACD金叉",
                    detected_at,
                    "macd",
                    {"dif": latest["macd_dif"], "dea": latest["macd_dea"], "histogram": latest["macd_histogram"]},
                    "DIF 由下向上穿越 DEA",
                    "DIF 继续位于 DEA 上方，柱状图继续改善",
                    "DIF 再次下穿 DEA",
                )
            )
        elif macd_cross == "death_cross":
            signals.append(
                _signal(
                    "MACD死叉",
                    detected_at,
                    "macd",
                    {"dif": latest["macd_dif"], "dea": latest["macd_dea"], "histogram": latest["macd_histogram"]},
                    "DIF 由上向下穿越 DEA",
                    "DIF 继续位于 DEA 下方，柱状图继续走弱",
                    "DIF 再次上穿 DEA",
                )
            )
        if rsi_state == "overbought":
            signals.append(
                _signal(
                    "RSI超买",
                    detected_at,
                    "rsi",
                    {"rsi14": latest["rsi14"], "threshold": 70.0},
                    "RSI14 大于或等于 70",
                    "RSI 维持强势且价格未出现明显背离",
                    "RSI 回落至 70 下方并伴随价格走弱",
                )
            )
        elif rsi_state == "oversold":
            signals.append(
                _signal(
                    "RSI超卖",
                    detected_at,
                    "rsi",
                    {"rsi14": latest["rsi14"], "threshold": 30.0},
                    "RSI14 小于或等于 30",
                    "RSI 自低位回升且价格不再创新低",
                    "RSI 继续下行且价格同步创新低",
                )
            )
        if latest["volume"] > latest["volume_ma20"] * 1.2:
            if latest["close"] > previous["close"]:
                signals.append(
                    _signal(
                        "放量上涨",
                        detected_at,
                        "volume_price",
                        {"latest_close": latest["close"], "previous_close": previous["close"], "volume": latest["volume"], "volume_ma20": latest["volume_ma20"]},
                        "成交量高于20日均量的1.2倍，且收盘价上涨",
                        "价格延续上行且成交量保持活跃",
                        "价格回落并吞没放量日涨幅",
                    )
                )
            elif latest["close"] < previous["close"]:
                signals.append(
                    _signal(
                        "放量下跌",
                        detected_at,
                        "volume_price",
                        {"latest_close": latest["close"], "previous_close": previous["close"], "volume": latest["volume"], "volume_ma20": latest["volume_ma20"]},
                        "成交量高于20日均量的1.2倍，且收盘价下跌",
                        "价格延续走弱且成交量保持活跃",
                        "价格收复放量日跌幅并站回关键价位",
                    )
                )

        patterns = [signal.name for signal in signals]

        output = TechnicalIndicators.model_validate(
            {
                "symbol": symbol,
                "as_of": as_of,
                "data_version": data_version,
                "script_version": script_version,
                "latest_price": _number(latest["close"]),
                "trend": {
                    "sma5": _number(latest["sma5"]),
                    "sma20": _number(latest["sma20"]),
                    "sma60": _number(latest["sma60"]),
                    "alignment": alignment,
                },
                "macd": {
                    "dif": _number(latest["macd_dif"]),
                    "dea": _number(latest["macd_dea"]),
                    "histogram": _number(latest["macd_histogram"]),
                    "cross": macd_cross,
                },
                "rsi": {"rsi14": _number(latest["rsi14"]), "state": rsi_state},
                "kdj": {
                    "k": _number(latest["kdj_k"]),
                    "d": _number(latest["kdj_d"]),
                    "j": _number(latest["kdj_j"]),
                    "cross": kdj_cross,
                },
                "bollinger": {
                    "upper": _number(latest["boll_upper"]),
                    "middle": _number(latest["boll_middle"]),
                    "lower": _number(latest["boll_lower"]),
                },
                "volatility": {
                    "atr14": _number(latest["atr14"]),
                    "annualized_volatility_20": _number(latest["annualized_volatility_20"]),
                },
                "volume": {
                    "latest": _number(latest["volume"]),
                    "ma5": _number(latest["volume_ma5"]),
                    "ma20": _number(latest["volume_ma20"]),
                },
                "support_resistance": {
                    "support_20": _number(low.tail(20).min()),
                    "support_60": _number(low.tail(60).min()),
                    "resistance_20": _number(high.tail(20).max()),
                    "resistance_60": _number(high.tail(60).max()),
                },
                "patterns": patterns,
                "signals": signals,
            }
        )
        return output, frame
    except IndicatorError:
        raise
    except Exception as exc:
        raise IndicatorError("技术指标计算失败") from exc


def atomic_write_json(model: TechnicalIndicators, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def generate_technical_chart(
    enriched: pd.DataFrame,
    indicators: TechnicalIndicators,
    path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    import matplotlib.dates as mdates

    plot = enriched.tail(180).copy()
    fig, (price_axis, volume_axis) = plt.subplots(
        2,
        1,
        figsize=(13.6, 8.4),
        sharex=True,
        gridspec_kw={"height_ratios": [3.15, 1], "hspace": 0.12},
    )
    fig.patch.set_facecolor("#ffffff")
    colors = {"close": "#2F80C1", "sma5": "#F28C38", "sma20": "#42A34B", "sma60": "#EF5350"}
    price_axis.plot(plot["date"], plot["close"], label="Close", color=colors["close"], linewidth=1.75)
    for window, color in ((5, colors["sma5"]), (20, colors["sma20"]), (60, colors["sma60"])):
        price_axis.plot(plot["date"], plot[f"sma{window}"], label=f"SMA{window}", color=color, linewidth=1.25)
    levels = indicators.support_resistance
    price_axis.axhline(levels.support_20, color="#3B8D67", linestyle="--", linewidth=1.25, label="Support 20")
    price_axis.axhline(levels.support_60, color="#2C6E4F", linestyle=":", linewidth=1.35, label="Support 60")
    price_axis.axhline(levels.resistance_20, color="#C4454D", linestyle="--", linewidth=1.25, label="Resistance 20")
    price_axis.axhline(levels.resistance_60, color="#9E3E49", linestyle=":", linewidth=1.35, label="Resistance 60")
    price_axis.set_title(f"{indicators.symbol} Technical Chart", fontsize=14, fontweight="medium", pad=9)
    price_axis.legend(loc="upper left", ncol=3, fontsize=8.5, frameon=True, framealpha=.92, edgecolor="#B8C0CC", borderpad=.55, handlelength=2.4)
    volume_axis.bar(plot["date"], plot["volume"], color="#718096", width=.82, alpha=.92)
    volume_axis.plot(plot["date"], plot["volume_ma20"], color=colors["sma5"], linewidth=1.25)
    volume_axis.set_ylabel("Volume", fontsize=10)
    volume_axis.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    volume_axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    for axis in (price_axis, volume_axis):
        axis.set_facecolor("#ffffff")
        axis.grid(True, color="#AAB3BF", alpha=.22, linewidth=.8)
        axis.tick_params(axis="both", labelsize=10, width=1, color="#202124")
        for spine in axis.spines.values():
            spine.set_color("#202124")
            spine.set_linewidth(1)
    plt.setp(volume_axis.get_xticklabels(), rotation=30, ha="right")
    fig.subplots_adjust(left=.075, right=.975, top=.925, bottom=.12)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fig.savefig(temporary, format="png", dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)
    os.replace(temporary, path)
