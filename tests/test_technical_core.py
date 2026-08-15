from __future__ import annotations

from datetime import date, timedelta
from importlib import import_module
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.technical.indicators import calculate_indicators
from app.technical.kronos import (
    KronosError,
    _kronos_runtime_spec,
    predict_kronos,
    validate_kronos_result,
)
from app.technical.market_data import (
    MarketDataError,
    _fetch_akshare_market_data,
    compute_data_version,
    get_market_data,
    resolve_security,
    load_persisted_market_data,
    validate_market_data,
)
from app.technical.schemas import PatternSignal


AS_OF = date(2026, 8, 5)


@pytest.mark.parametrize(
    ("query", "symbol"),
    [
        ("600519", "600519.SH"),
        ("600519.sh", "600519.SH"),
        ("SH600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("430047", "430047.BJ"),
        ("贵州茅台", "600519.SH"),
    ],
)
def test_resolve_security_mock(query: str, symbol: str, settings: Settings) -> None:
    result = resolve_security(query, settings)
    assert result.symbol == symbol


def test_resolve_security_rejects_unknown_name(settings: Settings) -> None:
    with pytest.raises(MarketDataError, match="SECURITY_NOT_FOUND"):
        resolve_security("不存在公司", settings)


@pytest.mark.parametrize("query", ["200001", "900901", "123456"])
def test_resolve_security_rejects_non_a_share_ranges(
    query: str, settings: Settings
) -> None:
    with pytest.raises(MarketDataError, match="SECURITY_INPUT_INVALID"):
        resolve_security(query, settings)


def test_resolve_security_rejects_ambiguous_exact_name(settings: Settings) -> None:
    with pytest.raises(MarketDataError, match="SECURITY_NAME_AMBIGUOUS"):
        resolve_security("测试重名", settings)


def test_mock_market_data_is_standard_and_deterministic(settings: Settings) -> None:
    first = get_market_data("600519.SH", AS_OF, settings)
    second = get_market_data("600519.SH", AS_OF, settings)
    assert list(first.columns) == [
        "date", "open", "high", "low", "close", "volume", "amount"
    ]
    assert len(first) == settings.market_data_lookback_days
    assert first["date"].max().date() <= AS_OF
    pd.testing.assert_frame_equal(first, second)


def test_market_data_validation_rejects_invalid_ohlc(settings: Settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    frame.loc[0, "high"] = frame.loc[0, "low"] - 1
    with pytest.raises(MarketDataError, match="MARKET_DATA_INVALID"):
        validate_market_data(frame, AS_OF, settings.market_data_min_bars)


@pytest.mark.parametrize(("column", "value"), [("volume", np.nan), ("amount", np.inf), ("open", -1)])
def test_market_data_validation_rejects_non_finite_or_nonpositive_values(
    column: str, value: float, settings: Settings
) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    frame.loc[0, column] = value
    with pytest.raises(MarketDataError, match="MARKET_DATA_INVALID"):
        validate_market_data(frame, AS_OF, settings.market_data_min_bars)


def test_mock_market_data_rejects_future_as_of(settings: Settings) -> None:
    with pytest.raises(MarketDataError, match="MARKET_DATA_INVALID"):
        get_market_data("600519.SH", date.today() + timedelta(days=1), settings)


def test_market_data_validation_rejects_insufficient_bars(settings: Settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings).tail(100)
    with pytest.raises(MarketDataError, match="MARKET_DATA_INSUFFICIENT"):
        validate_market_data(frame, AS_OF, settings.market_data_min_bars)


def test_akshare_market_data_falls_back_to_sina(settings: Settings, monkeypatch) -> None:
    expected = pd.DataFrame(
        {
            "date": [AS_OF],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.0],
            "volume": [100.0],
            "amount": [100.0],
        }
    )
    calls: list[tuple[str, str]] = []

    def eastmoney(**_kwargs):
        calls.append(("eastmoney", ""))
        raise ConnectionError("primary unavailable")

    def sina(*, symbol, **_kwargs):
        calls.append(("sina", symbol))
        return expected

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist=eastmoney, stock_zh_a_daily=sina),
    )

    result = _fetch_akshare_market_data("600519.SH", AS_OF, settings)

    assert result is expected
    assert calls == [("eastmoney", ""), ("sina", "sh600519")]


def _build_stock_list_frame() -> pd.DataFrame:
    """与真实 ak.stock_info_a_code_name 列结构一致的合成证券列表。"""
    return pd.DataFrame(
        {
            "code": ["601899", "600519"],
            "name": ["紫金矿业", "贵州茅台"],
        }
    )


def test_resolve_security_retries_transient_akshare_name_lookup(
    settings: Settings, monkeypatch
) -> None:
    """中文名解析在 live 模式应对 ak.stock_info_a_code_name 的瞬时失败退避重试。

    回归：历史 run 在 resolve_security 阶段因一次 AKShare 抖动直接 MARKET_DATA_FAILED
    掐死（中文名路径无重试、代码路径走离线 MOCK 永不触发）。现要求前 N 次失败、第 N+1
    次成功时 resolve_security 最终成功返回。
    """
    live = settings.model_copy(
        update={"market_data_mode": "live", "market_data_max_retries": 2}
    )
    calls = {"n": 0}

    def flaky_stock_info_a_code_name():
        calls["n"] += 1
        if calls["n"] < 3:  # 前两次瞬时失败
            raise ConnectionError("akshare list unavailable")
        return _build_stock_list_frame()

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_info_a_code_name=flaky_stock_info_a_code_name),
    )
    monkeypatch.setattr("app.technical.market_data.time.sleep", lambda _s: None)

    result = resolve_security("紫金矿业", live)

    assert result.symbol == "601899.SH"
    assert result.security_name == "紫金矿业"
    assert calls["n"] == 3


def test_resolve_security_persistent_akshare_failure_raises_market_data_failed(
    settings: Settings, monkeypatch
) -> None:
    """ak.stock_info_a_code_name 持续失败时，耗尽重试后仍以 MARKET_DATA_FAILED 失败，不降级。"""
    live = settings.model_copy(
        update={"market_data_mode": "live", "market_data_max_retries": 2}
    )

    def always_failing():
        raise ConnectionError("akshare list unavailable")

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_info_a_code_name=always_failing),
    )
    monkeypatch.setattr("app.technical.market_data.time.sleep", lambda _s: None)

    with pytest.raises(MarketDataError, match="MARKET_DATA_FAILED"):
        resolve_security("紫金矿业", live)


def test_data_version_is_repeatable(settings: Settings, tmp_path) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    csv_path = tmp_path / "market_data.csv"
    frame.to_csv(csv_path, index=False)
    assert compute_data_version("600519.SH", AS_OF, csv_path) == compute_data_version(
        "600519.SH", AS_OF, csv_path
    )


def test_persisted_market_data_rejects_sha_mismatch(settings: Settings, tmp_path) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    csv_path = tmp_path / "market_data.csv"
    frame.to_csv(csv_path, index=False)
    version = compute_data_version("600519.SH", AS_OF, csv_path)
    csv_path.write_text(csv_path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(MarketDataError, match="SHA256"):
        load_persisted_market_data(
            csv_path,
            symbol="600519.SH",
            as_of=AS_OF,
            expected_data_version=version,
        )


def test_indicators_match_reference_formulas(settings: Settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    output, enriched = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="v1",
        script_version="tech_indicator_v1",
    )
    close = frame["close"]
    assert output.trend.sma5 == pytest.approx(close.tail(5).mean())
    assert output.trend.sma20 == pytest.approx(close.tail(20).mean())
    assert output.trend.sma60 == pytest.approx(close.tail(60).mean())
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    assert output.macd.dif == pytest.approx(dif.iloc[-1])
    assert output.macd.dea == pytest.approx(dea.iloc[-1])
    assert output.macd.histogram == pytest.approx((dif.iloc[-1] - dea.iloc[-1]) * 2)
    assert output.bollinger.middle == pytest.approx(close.tail(20).mean())
    boll_std = close.rolling(20).std(ddof=0).iloc[-1]
    assert output.bollinger.upper == pytest.approx(close.tail(20).mean() + 2 * boll_std)
    assert output.bollinger.lower == pytest.approx(close.tail(20).mean() - 2 * boll_std)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    expected_rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    assert output.rsi.rsi14 == pytest.approx(expected_rsi.iloc[-1])
    lowest9 = frame["low"].rolling(9).min()
    highest9 = frame["high"].rolling(9).max()
    rsv = ((close - lowest9) / (highest9 - lowest9) * 100).fillna(50)
    expected_k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    expected_d = expected_k.ewm(alpha=1 / 3, adjust=False).mean()
    assert output.kdj.k == pytest.approx(expected_k.iloc[-1])
    assert output.kdj.d == pytest.approx(expected_d.iloc[-1])
    assert output.kdj.j == pytest.approx(3 * expected_k.iloc[-1] - 2 * expected_d.iloc[-1])
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    assert output.volatility.atr14 == pytest.approx(true_range.tail(14).mean())
    expected_volatility = close.pct_change().tail(20).std(ddof=0) * np.sqrt(252)
    assert output.volatility.annualized_volatility_20 == pytest.approx(
        expected_volatility, abs=1e-6
    )
    assert output.support_resistance.support_20 == pytest.approx(
        frame["low"].tail(20).min()
    )
    assert output.support_resistance.resistance_60 == pytest.approx(
        frame["high"].tail(60).max()
    )
    assert np.isfinite(output.rsi.rsi14)
    assert np.isfinite(output.kdj.k)
    assert np.isfinite(output.volatility.atr14)
    assert np.isfinite(output.volatility.annualized_volatility_20)
    assert {"sma5", "macd_dif", "rsi14", "atr14"}.issubset(enriched.columns)


def test_pattern_candidates_are_determined_by_code() -> None:
    count = 140
    close = np.arange(100, 100 + count, dtype=float)
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(end=AS_OF, periods=count),
            "open": close - 0.2,
            "high": close + 0.4,
            "low": close - 0.6,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, count),
            "amount": close * np.linspace(1_000_000, 2_000_000, count),
        }
    )
    output, _ = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="pattern-v1",
        script_version="tech_indicator_v1",
    )
    assert "20日突破" in output.patterns
    assert "均线多头排列" in output.patterns
    assert "RSI超买" in output.patterns


def test_detected_patterns_include_structured_signal_details() -> None:
    count = 140
    close = np.arange(100, 100 + count, dtype=float)
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(end=AS_OF, periods=count),
            "open": close - 0.2,
            "high": close + 0.4,
            "low": close - 0.6,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, count),
            "amount": close * np.linspace(1_000_000, 2_000_000, count),
        }
    )

    output, _ = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="pattern-signals-v1",
        script_version="tech_indicator_v1",
    )

    assert [signal.name for signal in output.signals] == output.patterns
    breakout = next(signal for signal in output.signals if signal.name == "20日突破")
    assert breakout.detected_at == AS_OF
    assert breakout.chart_family == "price_trend"
    assert {"latest_close", "prior_20_high"}.issubset(breakout.trigger_values)
    assert breakout.trigger_rule
    assert breakout.confirmation_rule
    assert breakout.invalidation_rule


def test_technical_visuals_include_market_overview_and_one_chart_per_detected_pattern() -> None:
    count = 140
    close = np.arange(100, 100 + count, dtype=float)
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(end=AS_OF, periods=count),
            "open": close - 0.2,
            "high": close + 0.4,
            "low": close - 0.6,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, count),
            "amount": close * np.linspace(1_000_000, 2_000_000, count),
        }
    )
    output, enriched = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="visuals-v1",
        script_version="tech_indicator_v1",
    )
    visuals_module = import_module("app.technical.visuals")

    visuals = visuals_module.build_technical_visuals(enriched, output)

    overview = next(
        chart for chart in visuals.charts
        if chart.chart_id == "technical-market-overview"
    )
    pattern_charts = [
        chart for chart in visuals.charts
        if chart.chart_id != "technical-market-overview"
    ]
    baseline_series = {
        "close", "sma5", "sma20", "sma60",
        "support20", "support60", "resistance20", "resistance60",
        "volume", "volume-ma20",
    }

    assert len(visuals.charts) == len(output.signals) + 1
    assert overview.plugin_id == "technical_market_overview"
    assert overview.annotations == []
    assert baseline_series.issubset({item.series_id for item in overview.series})
    assert {chart.annotations[0].label for chart in pattern_charts} == set(output.patterns)
    assert all(len(chart.annotations) == 1 for chart in pattern_charts)
    assert len({chart.chart_id for chart in visuals.charts}) == len(visuals.charts)
    price_signals = [
        signal for signal in output.signals if signal.chart_family == "price_trend"
    ]
    assert len(price_signals) >= 2
    for signal in price_signals:
        chart = next(
            item for item in pattern_charts
            if item.annotations[0].label == signal.name
        )
        assert signal.name in chart.title
        assert signal.name in chart.explanation
        assert baseline_series.issubset({item.series_id for item in chart.series})
        annotation = chart.annotations[0]
        assert signal.confirmation_rule in annotation.detail
        assert signal.invalidation_rule in annotation.detail


def test_technical_visuals_generate_market_overview_when_no_pattern_is_detected(settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    output, enriched = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="no-signals-v1",
        script_version="tech_indicator_v1",
    )
    output = output.model_copy(update={"patterns": [], "signals": []})
    visuals_module = import_module("app.technical.visuals")

    visuals = visuals_module.build_technical_visuals(enriched, output)

    assert len(visuals.charts) == 1
    overview = visuals.charts[0]
    assert overview.chart_id == "technical-market-overview"
    assert overview.annotations == []
    assert "X 轴最多显示 6 个等间隔日期" in overview.rendering_notes
    assert "Y 轴每个分区最多 4 个刻度" in overview.rendering_notes
    assert {
        "close", "sma5", "sma20", "sma60",
        "support20", "support60", "resistance20", "resistance60",
        "volume", "volume-ma20",
    }.issubset({item.series_id for item in overview.series})


def test_technical_pattern_charts_add_their_matching_indicator_panel(settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    output, enriched = calculate_indicators(
        frame,
        symbol="600519.SH",
        as_of=AS_OF,
        data_version="pattern-panels-v1",
        script_version="tech_indicator_v1",
    )
    signals = [
        PatternSignal(
            name="MACD金叉",
            detected_at=AS_OF,
            chart_family="macd",
            trigger_values={"dif": output.macd.dif, "dea": output.macd.dea},
            trigger_rule="DIF 上穿 DEA",
            confirmation_rule="DIF 保持在 DEA 上方",
            invalidation_rule="DIF 重新下穿 DEA",
        ),
        PatternSignal(
            name="RSI超买",
            detected_at=AS_OF,
            chart_family="rsi",
            trigger_values={"rsi14": output.rsi.rsi14, "threshold": 70.0},
            trigger_rule="RSI14 高于 70",
            confirmation_rule="RSI 保持强势",
            invalidation_rule="RSI 回落至 70 下方",
        ),
    ]
    visuals_module = import_module("app.technical.visuals")

    visuals = visuals_module.build_technical_visuals(
        enriched,
        output.model_copy(update={"patterns": [item.name for item in signals], "signals": signals}),
    )
    chart_by_pattern = {
        chart.annotations[0].label: chart
        for chart in visuals.charts
        if chart.annotations
    }

    assert {"macd-dif", "macd-dea", "macd-hist"}.issubset(
        {item.series_id for item in chart_by_pattern["MACD金叉"].series}
    )
    assert {"rsi14", "rsi70", "rsi30"}.issubset(
        {item.series_id for item in chart_by_pattern["RSI超买"].series}
    )


def test_kronos_mock_is_deterministic(settings: Settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    first = predict_kronos(frame, "600519.SH", AS_OF, "version", settings)
    second = predict_kronos(frame, "600519.SH", AS_OF, "version", settings)
    assert first == second
    assert sum(first.direction_probability.model_dump().values()) == pytest.approx(1)
    assert first.expected_return_range[0] <= first.expected_return_range[1]


def test_kronos_validation_checks_data_version(settings: Settings) -> None:
    frame = get_market_data("600519.SH", AS_OF, settings)
    result = predict_kronos(frame, "600519.SH", AS_OF, "version", settings)
    with pytest.raises(KronosError, match="KRONOS_FAILED"):
        validate_kronos_result(result, "600519.SH", "different")


def test_kronos_live_failure_does_not_fallback(settings: Settings) -> None:
    live = settings.model_copy(update={"kronos_mode": "live", "kronos_model_name": ""})
    frame = get_market_data("600519.SH", AS_OF, settings)
    with pytest.raises(KronosError, match="KRONOS_FAILED"):
        predict_kronos(frame, "600519.SH", AS_OF, "version", live)


def test_kronos_mini_uses_2k_tokenizer_and_full_context(settings: Settings) -> None:
    live = settings.model_copy(
        update={
            "kronos_mode": "live",
            "kronos_model_name": "NeoQuasar/Kronos-mini",
            "kronos_device": "mps",
        }
    )

    spec = _kronos_runtime_spec(live)

    assert spec.tokenizer_name == "NeoQuasar/Kronos-Tokenizer-2k"
    assert spec.max_context == 2048
    assert spec.device == "mps"
