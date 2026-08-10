from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.config import Settings
from app.technical.schemas import KronosResult


class KronosError(RuntimeError):
    code = "KRONOS_FAILED"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(f"{self.code}: {message}")


_MODEL_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


@dataclass(frozen=True)
class KronosRuntimeSpec:
    model_name: str
    tokenizer_name: str
    max_context: int
    device: str
    source_dir: Path


def _kronos_runtime_spec(settings: Settings) -> KronosRuntimeSpec:
    is_mini = "mini" in settings.kronos_model_name.lower()
    return KronosRuntimeSpec(
        model_name=settings.kronos_model_name,
        tokenizer_name=(
            "NeoQuasar/Kronos-Tokenizer-2k"
            if is_mini
            else "NeoQuasar/Kronos-Tokenizer-base"
        ),
        max_context=2048 if is_mini else 512,
        device=settings.kronos_device,
        source_dir=settings.kronos_source_dir.resolve(),
    )


def predict_kronos(
    market_data: pd.DataFrame,
    symbol: str,
    as_of: date,
    data_version: str,
    settings: Settings | None = None,
) -> KronosResult:
    settings = settings or Settings.from_env()
    try:
        if settings.kronos_mode == "mock":
            result = _predict_mock(market_data, symbol, as_of, data_version, settings)
        else:
            result = _predict_live_with_timeout(
                market_data, symbol, as_of, data_version, settings
            )
        validate_kronos_result(result, symbol, data_version)
        return result
    except KronosError:
        raise
    except Exception as exc:
        raise KronosError("Kronos 预测执行失败", retryable=True) from exc


def _predict_live_with_timeout(
    market_data: pd.DataFrame,
    symbol: str,
    as_of: date,
    data_version: str,
    settings: Settings,
) -> KronosResult:
    if not _INFERENCE_LOCK.acquire(blocking=False):
        raise KronosError("已有 Kronos Live 推理尚未结束")
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run_inference() -> None:
        try:
            outcome: tuple[bool, Any] = (
                True,
                _predict_live(
                    market_data, symbol, as_of, data_version, settings
                ),
            )
        except BaseException as exc:
            outcome = (False, exc)
        finally:
            _INFERENCE_LOCK.release()
        results.put(outcome)

    # A timed-out native model call cannot be safely killed inside CPython.
    # A daemon single-flight thread prevents concurrent predictor use and does
    # not keep Worker shutdown hostage. Timeouts are deliberately non-retryable.
    threading.Thread(
        target=run_inference,
        name="kronos-live-inference",
        daemon=True,
    ).start()
    try:
        succeeded, value = results.get(timeout=settings.kronos_timeout_seconds)
    except queue.Empty as exc:
        raise KronosError("Kronos 模型推理超时") from exc
    if succeeded:
        return value
    raise value


def _predict_mock(
    market_data: pd.DataFrame,
    symbol: str,
    as_of: date,
    data_version: str,
    settings: Settings,
) -> KronosResult:
    close = market_data["close"].astype(float)
    returns = close.pct_change().dropna().tail(min(settings.kronos_lookback, 120))
    momentum = float(close.iloc[-1] / close.iloc[-21] - 1)
    volatility = max(float(returns.std(ddof=0) * np.sqrt(20)), 0.005)
    score = float(np.tanh(momentum / max(volatility, 0.01)))
    up = 0.34 + 0.16 * max(score, 0)
    down = 0.26 + 0.16 * max(-score, 0)
    flat = 1 - up - down
    probabilities = np.array([up, flat, down], dtype=float)
    probabilities /= probabilities.sum()
    expected = momentum * 0.35
    lower = expected - volatility * 1.25
    upper = expected + volatility * 1.25
    confidence = min(0.75, max(0.35, 0.45 + abs(score) * 0.2))
    return KronosResult(
        symbol=symbol,
        as_of=as_of,
        horizon=f"{settings.kronos_prediction_length}_trading_days",
        direction_probability={
            "up": round(float(probabilities[0]), 6),
            "flat": round(float(probabilities[1]), 6),
            "down": round(float(probabilities[2]), 6),
        },
        expected_return_range=(round(lower, 6), round(upper, 6)),
        model_confidence=round(confidence, 6),
        model_version="mock_kronos_v1",
        data_version=data_version,
    )


def _get_live_predictor(settings: Settings):
    if not settings.kronos_model_name.strip():
        raise KronosError("KRONOS_MODEL_NAME 未配置，Live 模式不能启动")
    spec = _kronos_runtime_spec(settings)
    runtime_device = spec.device
    if runtime_device == "mps":
        try:
            import torch

            if not torch.backends.mps.is_available():
                runtime_device = "cpu"
        except (ImportError, AttributeError):
            runtime_device = "cpu"
    cache_key = (spec.model_name, runtime_device)
    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            return _MODEL_CACHE[cache_key]
        if not (spec.source_dir / "model" / "__init__.py").is_file():
            raise KronosError("KRONOS_SOURCE_DIR 中未找到官方 model 包")
        source_path = str(spec.source_dir)
        if source_path not in sys.path:
            sys.path.insert(0, source_path)
        try:
            from model import Kronos, KronosPredictor, KronosTokenizer
        except ImportError as exc:
            raise KronosError("未安装官方 Kronos 源码及其依赖") from exc
        try:
            tokenizer = KronosTokenizer.from_pretrained(spec.tokenizer_name)
            model = Kronos.from_pretrained(spec.model_name)
            if hasattr(model, "to"):
                model = model.to(runtime_device)
            predictor = KronosPredictor(
                model,
                tokenizer,
                device=runtime_device,
                max_context=spec.max_context,
            )
        except Exception as exc:
            raise KronosError("Kronos 模型加载失败", retryable=True) from exc
        _MODEL_CACHE[cache_key] = predictor
        return predictor


def _predict_live(
    market_data: pd.DataFrame,
    symbol: str,
    as_of: date,
    data_version: str,
    settings: Settings,
) -> KronosResult:
    predictor = _get_live_predictor(settings)
    history = market_data.tail(settings.kronos_lookback).copy()
    features = history[["open", "high", "low", "close", "volume", "amount"]]
    x_timestamp = pd.to_datetime(history["date"])
    y_timestamp = pd.bdate_range(
        start=pd.Timestamp(as_of) + pd.offsets.BDay(1),
        periods=settings.kronos_prediction_length,
    )
    try:
        forecast = predictor.predict(
            df=features,
            x_timestamp=x_timestamp,
            y_timestamp=pd.Series(y_timestamp),
            pred_len=settings.kronos_prediction_length,
            T=1.0,
            top_p=0.9,
            sample_count=5,
        )
        predicted_close = pd.to_numeric(forecast["close"], errors="raise")
    except Exception as exc:
        raise KronosError("Kronos 模型推理失败", retryable=True) from exc
    current = float(history["close"].iloc[-1])
    path_returns = predicted_close / current - 1
    terminal_return = float(path_returns.iloc[-1])
    scale = max(float(path_returns.std(ddof=0)), 0.01)
    score = float(np.tanh(terminal_return / scale))
    up = 0.34 + 0.2 * max(score, 0)
    down = 0.26 + 0.2 * max(-score, 0)
    flat = 1 - up - down
    probabilities = np.array([up, flat, down])
    probabilities /= probabilities.sum()
    return KronosResult(
        symbol=symbol,
        as_of=as_of,
        horizon=f"{settings.kronos_prediction_length}_trading_days",
        direction_probability={
            "up": round(float(probabilities[0]), 6),
            "flat": round(float(probabilities[1]), 6),
            "down": round(float(probabilities[2]), 6),
        },
        expected_return_range=(
            round(float(path_returns.min()), 6),
            round(float(path_returns.max()), 6),
        ),
        model_confidence=round(min(0.8, 0.45 + abs(score) * 0.25), 6),
        model_version=settings.kronos_model_name,
        data_version=data_version,
    )


def validate_kronos_result(
    result: KronosResult, expected_symbol: str, expected_data_version: str
) -> None:
    if result.symbol != expected_symbol or result.data_version != expected_data_version:
        raise KronosError("Kronos 输出与当前证券或数据版本不一致")
    probabilities = result.direction_probability.model_dump().values()
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities):
        raise KronosError("Kronos 方向概率越界")
    if abs(sum(probabilities) - 1) > 1e-6:
        raise KronosError("Kronos 方向概率之和不为 1")
    lower, upper = result.expected_return_range
    if (
        not math.isfinite(lower)
        or not math.isfinite(upper)
        or not math.isfinite(result.model_confidence)
        or lower > upper
        or not 0 <= result.model_confidence <= 1
    ):
        raise KronosError("Kronos 收益区间或置信度非法")


def atomic_write_kronos(result: KronosResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
