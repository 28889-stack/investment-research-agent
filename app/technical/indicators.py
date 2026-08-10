from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.technical.schemas import TechnicalIndicators


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

        patterns: list[str] = []
        prior20 = frame.iloc[-21:-1]
        if latest["close"] > prior20["high"].max():
            patterns.append("20日突破")
        if latest["close"] < prior20["low"].min():
            patterns.append("20日跌破")
        if alignment == "bullish":
            patterns.append("均线多头排列")
        elif alignment == "bearish":
            patterns.append("均线空头排列")
        if macd_cross == "golden_cross":
            patterns.append("MACD金叉")
        elif macd_cross == "death_cross":
            patterns.append("MACD死叉")
        if rsi_state == "overbought":
            patterns.append("RSI超买")
        elif rsi_state == "oversold":
            patterns.append("RSI超卖")
        if latest["volume"] > latest["volume_ma20"] * 1.2:
            if latest["close"] > previous["close"]:
                patterns.append("放量上涨")
            elif latest["close"] < previous["close"]:
                patterns.append("放量下跌")

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

    plot = enriched.tail(180).copy()
    fig, (price_axis, volume_axis) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    price_axis.plot(plot["date"], plot["close"], label="Close", linewidth=1.4)
    for window in (5, 20, 60):
        price_axis.plot(plot["date"], plot[f"sma{window}"], label=f"SMA{window}", linewidth=1)
    levels = indicators.support_resistance
    price_axis.axhline(levels.support_20, color="#2f855a", linestyle="--", label="Support 20")
    price_axis.axhline(levels.support_60, color="#276749", linestyle=":", label="Support 60")
    price_axis.axhline(levels.resistance_20, color="#c53030", linestyle="--", label="Resistance 20")
    price_axis.axhline(levels.resistance_60, color="#9b2c2c", linestyle=":", label="Resistance 60")
    price_axis.set_title(f"{indicators.symbol} Technical Chart")
    price_axis.legend(loc="upper left", ncol=3, fontsize=8)
    price_axis.grid(alpha=0.2)
    volume_axis.bar(plot["date"], plot["volume"], color="#718096", width=1)
    volume_axis.plot(plot["date"], plot["volume_ma20"], color="#dd6b20", linewidth=1)
    volume_axis.set_ylabel("Volume")
    fig.autofmt_xdate()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fig.savefig(temporary, format="png", dpi=140)
    plt.close(fig)
    os.replace(temporary, path)
