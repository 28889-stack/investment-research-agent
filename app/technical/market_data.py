from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np
import pandas as pd

from app.config import Settings
from app.technical.schemas import ResolvedSecurity


STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
MOCK_SECURITIES = (
    ("600519", "SH", "贵州茅台"),
    ("000001", "SZ", "平安银行"),
    ("300750", "SZ", "宁德时代"),
    ("430047", "BJ", "诺思兰德"),
    ("600001", "SH", "测试重名"),
    ("000002", "SZ", "测试重名"),
)
CODE_PATTERNS = (
    re.compile(r"^(?P<ticker>\d{6})(?:\.(?P<suffix>SH|SZ|BJ))?$", re.I),
    re.compile(r"^(?P<prefix>SH|SZ|BJ)(?P<ticker>\d{6})$", re.I),
)


class MarketDataError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _infer_exchange(ticker: str) -> str:
    if ticker.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if ticker.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if ticker.startswith(("4", "8", "920")):
        return "BJ"
    raise MarketDataError("SECURITY_INPUT_INVALID", "不支持的 A 股证券代码")


def resolve_security(query: str, settings: Settings | None = None) -> ResolvedSecurity:
    settings = settings or Settings.from_env()
    cleaned = query.strip()
    if not cleaned or len(cleaned) > 64:
        raise MarketDataError("SECURITY_INPUT_INVALID", "证券输入为空或过长")
    for pattern in CODE_PATTERNS:
        match = pattern.fullmatch(cleaned)
        if not match:
            continue
        ticker = match.group("ticker")
        supplied = (match.groupdict().get("prefix") or match.groupdict().get("suffix"))
        inferred = _infer_exchange(ticker)
        if supplied and supplied.upper() != inferred:
            raise MarketDataError("SECURITY_INPUT_INVALID", "证券代码与交易所不一致")
        name = next(
            (item[2] for item in MOCK_SECURITIES if item[0] == ticker and item[1] == inferred),
            ticker,
        )
        return ResolvedSecurity(
            ticker=ticker,
            exchange=inferred,
            symbol=f"{ticker}.{inferred}",
            security_name=name,
        )

    matches = _security_name_matches(cleaned, settings)
    if not matches:
        raise MarketDataError("SECURITY_NOT_FOUND", "未找到精确匹配的 A 股证券")
    if len(matches) > 1:
        raise MarketDataError("SECURITY_NAME_AMBIGUOUS", "证券名称存在多个精确匹配")
    ticker, exchange, name = matches[0]
    return ResolvedSecurity(
        ticker=ticker,
        exchange=exchange,
        symbol=f"{ticker}.{exchange}",
        security_name=name,
    )


def _security_name_matches(name: str, settings: Settings) -> list[tuple[str, str, str]]:
    if settings.market_data_mode == "mock":
        return [item for item in MOCK_SECURITIES if item[2] == name]

    def _fetch_stock_list() -> pd.DataFrame:
        # 原始异常必须在重试边界*内部*转换为 ``MARKET_DATA_FAILED``，与
        # ``_fetch_akshare_market_data`` 同构，否则 ``_retry_generic`` 只认
        # ``MarketDataError``、对 akshare 原生异常不会重试（历史 MARKET_DATA_FAILED
        # 复现根因即在此：一次抖动直接掐死 run）。
        try:
            import akshare as ak

            return ak.stock_info_a_code_name()
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError("MARKET_DATA_FAILED", "AKShare 证券列表获取失败") from exc

    stock_list = _retry_generic(_fetch_stock_list, settings.market_data_max_retries)
    columns = {str(column): column for column in stock_list.columns}
    code_column = columns.get("code") or columns.get("代码")
    name_column = columns.get("name") or columns.get("名称")
    if code_column is None or name_column is None:
        raise MarketDataError("MARKET_DATA_INVALID", "AKShare 证券列表列名异常")
    matches: list[tuple[str, str, str]] = []
    for _, row in stock_list.loc[stock_list[name_column].astype(str) == name].iterrows():
        ticker = str(row[code_column]).zfill(6)
        matches.append((ticker, _infer_exchange(ticker), str(row[name_column])))
    return matches


def get_market_data(
    symbol: str, as_of: date, settings: Settings | None = None
) -> pd.DataFrame:
    settings = settings or Settings.from_env()
    if as_of > date.today():
        raise MarketDataError("MARKET_DATA_INVALID", "数据截止日期不能晚于今天")
    if settings.market_data_mode == "mock":
        frame = load_mock_market_data(symbol, as_of, settings.market_data_lookback_days)
    else:
        if settings.market_data_provider != "akshare":
            raise MarketDataError("MARKET_DATA_FAILED", "仅支持 akshare 行情源")
        frame = _retry(
            lambda: _fetch_akshare_market_data(symbol, as_of, settings),
            settings.market_data_max_retries,
        )
    frame = _normalize_market_data(frame, as_of)
    frame = frame.tail(settings.market_data_lookback_days).reset_index(drop=True)
    validate_market_data(frame, as_of, settings.market_data_min_bars)
    return frame


def load_mock_market_data(
    symbol: str, as_of: date, lookback_days: int = 750
) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(as_of), periods=lookback_days)
    index = np.arange(lookback_days, dtype=float)
    # One generic deterministic process is used for every symbol. The ticker is
    # deliberately not used to assign a direction or conclusion.
    close = 80 + index * 0.035 + np.sin(index / 13) * 2.4 + np.cos(index / 31) * 1.2
    open_price = close * (1 + np.sin(index / 7) * 0.002)
    high = np.maximum(open_price, close) * (1.008 + np.abs(np.sin(index / 17)) * 0.004)
    low = np.minimum(open_price, close) * (0.992 - np.abs(np.cos(index / 19)) * 0.003)
    volume = 1_000_000 + (np.sin(index / 9) + 1.2) * 180_000 + index * 100
    amount = volume * close
    return pd.DataFrame(
        {
            "date": dates,
            "open": np.round(open_price, 4),
            "high": np.round(high, 4),
            "low": np.round(low, 4),
            "close": np.round(close, 4),
            "volume": np.round(volume, 2),
            "amount": np.round(amount, 2),
        }
    )


def _fetch_akshare_market_data(
    symbol: str, as_of: date, settings: Settings
) -> pd.DataFrame:
    try:
        import akshare as ak

        ticker = symbol.split(".", 1)[0]
        start = as_of - timedelta(days=max(settings.market_data_lookback_days * 2, 1000))
        try:
            primary = ak.stock_zh_a_hist(
                symbol=ticker,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=as_of.strftime("%Y%m%d"),
                adjust=settings.market_data_adjustment,
            )
            if not primary.empty:
                return primary
        except Exception:
            pass
        exchange = symbol.partition(".")[2].lower()
        fallback = ak.stock_zh_a_daily(
            symbol=f"{exchange}{ticker}",
            start_date=start.strftime("%Y%m%d"),
            end_date=as_of.strftime("%Y%m%d"),
            adjust=settings.market_data_adjustment,
        )
        if fallback.empty:
            raise ValueError("AKShare 两个日线接口均返回空数据")
        return fallback
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError("MARKET_DATA_FAILED", "AKShare 日线行情获取失败") from exc


def _retry(operation: Callable[[], pd.DataFrame], max_retries: int) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except MarketDataError as exc:
            last_error = exc
            if exc.code != "MARKET_DATA_FAILED" or attempt >= max_retries:
                raise
            time.sleep(min(0.2 * (attempt + 1), 0.5))
    assert last_error is not None
    raise last_error


_T = TypeVar("_T")


def _retry_generic(operation: Callable[[], _T], max_retries: int) -> _T:
    """对瞬时 ``MARKET_DATA_FAILED`` 做有限次退避重试，与 ``_retry`` 同语义但泛型。

    用于 ``_security_name_matches`` 的 AKShare 证券列表拉取，使中文名解析在
    ``resolve_security`` 阶段获得与 ``get_market_data`` 一致的瞬时网络容忍度，
    避免一次抖动直接掐死整个 run（见 MARKET_DATA_FAILED 历史复现）。
    """
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except MarketDataError as exc:
            last_error = exc
            if exc.code != "MARKET_DATA_FAILED" or attempt >= max_retries:
                raise
            time.sleep(min(0.2 * (attempt + 1), 0.5))
    assert last_error is not None
    raise last_error


def _normalize_market_data(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    aliases = {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "成交额": "amount",
    }
    normalized = frame.rename(columns=aliases).copy()
    missing = set(STANDARD_COLUMNS) - set(normalized.columns)
    if missing:
        raise MarketDataError("MARKET_DATA_INVALID", f"行情缺少标准列: {sorted(missing)}")
    normalized = normalized[STANDARD_COLUMNS]
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in STANDARD_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.loc[normalized["date"].dt.date <= as_of]
    return normalized.sort_values("date", kind="stable").reset_index(drop=True)


def validate_market_data(frame: pd.DataFrame, as_of: date, min_bars: int = 120) -> None:
    if frame.empty:
        raise MarketDataError("MARKET_DATA_INVALID", "行情数据为空")
    if set(STANDARD_COLUMNS) - set(frame.columns):
        raise MarketDataError("MARKET_DATA_INVALID", "行情标准列不完整")
    if len(frame) < min_bars:
        raise MarketDataError("MARKET_DATA_INSUFFICIENT", f"行情不足 {min_bars} 根")
    if frame["date"].isna().any() or not frame["date"].is_monotonic_increasing:
        raise MarketDataError("MARKET_DATA_INVALID", "行情日期非法或未升序")
    if frame["date"].duplicated().any():
        raise MarketDataError("MARKET_DATA_INVALID", "行情日期重复")
    numeric = frame[STANDARD_COLUMNS[1:]].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise MarketDataError("MARKET_DATA_INVALID", "行情数值包含空值或非有限值")
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any():
        raise MarketDataError("MARKET_DATA_INVALID", "high 小于 OHLC 其他价格")
    if (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        raise MarketDataError("MARKET_DATA_INVALID", "low 大于 OHLC 其他价格")
    if (
        (frame[["open", "high", "low", "close"]] <= 0).any().any()
        or (frame["volume"] < 0).any()
        or (frame["amount"] < 0).any()
    ):
        raise MarketDataError("MARKET_DATA_INVALID", "OHLC、成交量或成交额非法")
    if (frame["date"].dt.date > as_of).any():
        raise MarketDataError("MARKET_DATA_INVALID", "行情包含截止日之后数据")


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    os.replace(temporary, path)


def compute_data_version(symbol: str, as_of: date, csv_path: Path) -> str:
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()[:8]
    return f"{symbol}_{as_of.strftime('%Y%m%d')}_{digest}"


def load_persisted_market_data(
    path: Path,
    *,
    symbol: str,
    as_of: date,
    expected_data_version: str,
    min_bars: int = 120,
) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path)
        frame = _normalize_market_data(frame, as_of)
        validate_market_data(frame, as_of, min_bars)
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError("MARKET_DATA_INVALID", "落盘行情无法解析") from exc
    actual_version = compute_data_version(symbol, as_of, path)
    if actual_version != expected_data_version:
        raise MarketDataError(
            "MARKET_DATA_INVALID", "落盘行情 SHA256 与 data_version 不一致"
        )
    return frame
