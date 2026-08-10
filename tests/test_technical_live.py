from __future__ import annotations

import importlib.util
import os
from datetime import date

import pytest

from app.config import Settings
from app.technical.kronos import predict_kronos
from app.technical.market_data import get_market_data


@pytest.mark.market_live
@pytest.mark.skipif(
    os.getenv("RUN_MARKET_LIVE") != "1",
    reason="设置 RUN_MARKET_LIVE=1 后执行真实 AKShare 验证",
)
def test_akshare_live_market_data() -> None:
    settings = Settings.from_env().model_copy(update={"market_data_mode": "live"})
    frame = get_market_data("600519.SH", date.today(), settings)
    assert len(frame) >= settings.market_data_min_bars
    assert frame["date"].max().date() <= date.today()


@pytest.mark.kronos_live
@pytest.mark.skipif(
    not os.getenv("KRONOS_MODEL_NAME") or importlib.util.find_spec("model") is None,
    reason="需要官方 Kronos 源码、依赖和 KRONOS_MODEL_NAME",
)
def test_kronos_live_prediction() -> None:
    base = Settings.from_env()
    mock_market = base.model_copy(update={"market_data_mode": "mock"})
    frame = get_market_data("600519.SH", date.today(), mock_market)
    live = base.model_copy(update={"kronos_mode": "live"})
    result = predict_kronos(frame, "600519.SH", date.today(), "live-test", live)
    assert result.model_version == live.kronos_model_name
    assert sum(result.direction_probability.model_dump().values()) == pytest.approx(1)
