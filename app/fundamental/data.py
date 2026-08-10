from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.config import Settings
from app.fundamental.schemas import CompanyProfile, FinancialData, FinancialPeriod, MarketSnapshot
from app.technical.market_data import resolve_security


class FundamentalDataError(RuntimeError):
    code = "FINANCIAL_DATA_FAILED"


class CompanyProfileError(FundamentalDataError):
    code = "COMPANY_PROFILE_FAILED"


class FinancialDataInvalidError(FundamentalDataError):
    code = "FINANCIAL_DATA_INVALID"


def _resolved(symbol: str):
    try:
        return resolve_security(symbol)
    except ValueError as exc:
        raise ValueError("symbol is not a supported A-share security") from exc


def get_company_profile(symbol: str, as_of: date, settings: Settings | Any) -> CompanyProfile:
    resolved = _resolved(symbol)
    mode = getattr(settings, "fundamental_data_mode", "mock")
    if mode == "mock":
        known = resolved.symbol == "600519.SH"
        return CompanyProfile(
            symbol=resolved.symbol,
            company_name=("贵州茅台酒股份有限公司" if known else resolved.security_name),
            short_name=("贵州茅台" if known else resolved.security_name),
            industry=("白酒" if known else "未分类"),
            listing_date=("2001-08-27" if known else ""),
            business_summary=("主要从事茅台酒及系列酒的生产与销售。" if known else "Mock 公司基础资料。"),
            currency="CNY",
            as_of=as_of,
            data_source="mock",
        )
    if mode != "live":
        raise FundamentalDataError(f"未知基本面数据模式: {mode}")
    return _get_live_company_profile(resolved.symbol, as_of, settings)


def get_financial_data(symbol: str, as_of: date, settings: Settings | Any) -> FinancialData:
    resolved = _resolved(symbol)
    mode = getattr(settings, "fundamental_data_mode", "mock")
    if mode == "mock":
        return _mock_financial_data(resolved.symbol, as_of)
    if mode != "live":
        raise FundamentalDataError(f"未知基本面数据模式: {mode}")
    return _get_live_financial_data(resolved.symbol, as_of, settings)


def _mock_financial_data(symbol: str, as_of: date) -> FinancialData:
    rows: list[FinancialPeriod] = []
    for index, year in enumerate(range(2020, 2026)):
        scale = 1 + index * 0.10
        published = date(year + 1, 3, 20)
        if published > as_of:
            continue
        revenue = 100_000_000_000 * scale
        net_profit = 48_000_000_000 * scale
        rows.append(
            FinancialPeriod(
                period=f"{year}-12-31",
                report_type="annual",
                published_date=published.isoformat(),
                revenue=revenue,
                operating_profit=60_000_000_000 * scale,
                net_profit=net_profit,
                net_profit_attributable=47_000_000_000 * scale,
                total_assets=210_000_000_000 * scale,
                total_liabilities=45_000_000_000 * scale,
                interest_bearing_debt=5_000_000_000 * scale,
                shareholders_equity=165_000_000_000 * scale,
                current_assets=185_000_000_000 * scale,
                current_liabilities=40_000_000_000 * scale,
                cash=120_000_000_000 * scale,
                accounts_receivable=1_000_000_000 * scale,
                inventory=30_000_000_000 * scale,
                operating_cash_flow=52_000_000_000 * scale,
                capital_expenditure=4_000_000_000 * scale,
                basic_eps=37.4 * scale,
                shares_outstanding=1_256_197_800.0,
            )
        )
    return FinancialData(
        symbol=symbol,
        as_of=as_of,
        currency="CNY",
        unit="CNY",
        data_source="mock",
        periods=rows[-5:],
    )


def _get_live_company_profile(symbol: str, as_of: date, settings: Any) -> CompanyProfile:
    provider = getattr(settings, "fundamental_data_provider", "akshare")
    if provider != "akshare":
        raise FundamentalDataError(f"不支持的基本面数据提供方: {provider}")
    try:
        import akshare as ak

        frame = _retry_call(
            lambda: ak.stock_profile_cninfo(symbol=symbol[:6]),
            getattr(settings, "fundamental_data_max_retries", 2),
        )
        return _map_live_company_profile(symbol, as_of, frame)
    except Exception as exc:
        raise CompanyProfileError("COMPANY_PROFILE_FAILED: AKShare 公司资料获取失败") from exc


def _map_live_company_profile(symbol: str, as_of: date, frame: pd.DataFrame) -> CompanyProfile:
    if frame.empty:
        raise CompanyProfileError("COMPANY_PROFILE_FAILED: AKShare 公司资料为空")
    values = frame.iloc[0]
    company_name = str(values.get("公司名称") or "").strip()
    short_name = str(values.get("A股简称") or "").strip()
    if not company_name or not short_name:
        raise CompanyProfileError("COMPANY_PROFILE_FAILED: 公司名称或简称缺失")
    return CompanyProfile(
        symbol=symbol,
        company_name=company_name,
        short_name=short_name,
        industry=str(values.get("所属行业") or "").strip(),
        listing_date=str(values.get("上市日期") or "").strip(),
        business_summary=str(values.get("主营业务") or "").strip(),
        currency="CNY",
        as_of=as_of,
        data_source="akshare",
    )


def _get_live_financial_data(symbol: str, as_of: date, settings: Any) -> FinancialData:
    provider = getattr(settings, "fundamental_data_provider", "akshare")
    if provider != "akshare":
        raise FundamentalDataError(f"不支持的基本面数据提供方: {provider}")
    try:
        import akshare as ak

        exchange_symbol = f"{symbol[-2:]}{symbol[:6]}"
        retries = getattr(settings, "fundamental_data_max_retries", 2)
        balance = _retry_call(lambda: ak.stock_balance_sheet_by_yearly_em(symbol=exchange_symbol), retries)
        profit = _retry_call(lambda: ak.stock_profit_sheet_by_yearly_em(symbol=exchange_symbol), retries)
        cash = _retry_call(lambda: ak.stock_cash_flow_sheet_by_yearly_em(symbol=exchange_symbol), retries)
        return _map_live_financial_frames(symbol, as_of, balance, profit, cash)
    except Exception as exc:
        if isinstance(exc, FundamentalDataError):
            raise
        raise FundamentalDataError("FINANCIAL_DATA_FAILED: AKShare 财务数据获取失败") from exc


def _map_live_financial_frames(
    symbol: str,
    as_of: date,
    balance: pd.DataFrame,
    profit: pd.DataFrame,
    cash: pd.DataFrame,
) -> FinancialData:
    frames = {
        "balance": _prepare_live_frame(balance, as_of),
        "profit": _prepare_live_frame(profit, as_of),
        "cash": _prepare_live_frame(cash, as_of),
    }
    periods = sorted(set(frames["balance"].index) & set(frames["profit"].index) & set(frames["cash"].index))[-5:]
    if not periods:
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: as_of 之前无可用年度三表数据")
    if len(periods) < 5:
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: 可用年度三表共同期间少于 5 年")
    rows: list[FinancialPeriod] = []
    dataset_currency: str | None = None
    for period in periods:
        balance_row = frames["balance"].loc[period]
        profit_row = frames["profit"].loc[period]
        cash_row = frames["cash"].loc[period]
        currencies = {
            str(row.get("CURRENCY", "")).strip()
            for row in (balance_row, profit_row, cash_row)
            if str(row.get("CURRENCY", "")).strip()
        }
        if len(currencies) != 1:
            raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: 三表币种缺失或不一致")
        period_currency = next(iter(currencies))
        if dataset_currency is not None and period_currency != dataset_currency:
            raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: 跨期币种不一致")
        dataset_currency = period_currency
        notice_dates = [pd.to_datetime(row["NOTICE_DATE"]).date() for row in (balance_row, profit_row, cash_row)]
        rows.append(
            FinancialPeriod(
                period=period,
                report_type=str(profit_row.get("REPORT_TYPE") or "annual"),
                published_date=max(notice_dates).isoformat(),
                revenue=_number(profit_row, "TOTAL_OPERATE_INCOME"),
                operating_profit=_number(profit_row, "OPERATE_PROFIT"),
                net_profit=_number(profit_row, "NETPROFIT"),
                net_profit_attributable=_number(profit_row, "PARENT_NETPROFIT"),
                total_assets=_number(balance_row, "TOTAL_ASSETS"),
                total_liabilities=_number(balance_row, "TOTAL_LIABILITIES"),
                interest_bearing_debt=_interest_bearing_debt(balance_row),
                shareholders_equity=_number(balance_row, "TOTAL_PARENT_EQUITY"),
                current_assets=_number(balance_row, "TOTAL_CURRENT_ASSETS"),
                current_liabilities=_number(balance_row, "TOTAL_CURRENT_LIAB"),
                cash=_number(balance_row, "MONETARYFUNDS"),
                accounts_receivable=_number(balance_row, "ACCOUNTS_RECE"),
                inventory=_number(balance_row, "INVENTORY"),
                operating_cash_flow=_number(cash_row, "NETCASH_OPERATE"),
                capital_expenditure=_number(cash_row, "CONSTRUCT_LONG_ASSET"),
                basic_eps=_number(profit_row, "BASIC_EPS"),
                shares_outstanding=_number(balance_row, "SHARE_CAPITAL"),
            )
        )
    return FinancialData(
        symbol=symbol,
        as_of=as_of,
        currency=dataset_currency or "CNY",
        unit=dataset_currency or "CNY",
        data_source="akshare",
        periods=rows,
    )


def _prepare_live_frame(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    required = {"REPORT_DATE", "NOTICE_DATE", "CURRENCY"}
    if frame.empty or required - set(frame.columns):
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: AKShare 财务表为空或缺少基础字段")
    normalized = frame.copy()
    normalized["REPORT_DATE"] = pd.to_datetime(normalized["REPORT_DATE"], errors="coerce")
    normalized["NOTICE_DATE"] = pd.to_datetime(normalized["NOTICE_DATE"], errors="coerce")
    normalized = normalized.loc[
        normalized["REPORT_DATE"].notna()
        & normalized["NOTICE_DATE"].notna()
        & (normalized["REPORT_DATE"].dt.date <= as_of)
        & (normalized["NOTICE_DATE"].dt.date <= as_of)
    ].copy()
    normalized["period_key"] = normalized["REPORT_DATE"].dt.date.astype(str)
    if normalized["period_key"].duplicated().any():
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: AKShare 年度财务期间重复")
    return normalized.set_index("period_key", drop=True)


def _number(row: pd.Series, key: str) -> float | None:
    value = row.get(key)
    if value is None or pd.isna(value):
        return None
    return float(value)


def _interest_bearing_debt(row: pd.Series) -> float | None:
    fields = (
        "SHORT_LOAN",
        "LONG_LOAN",
        "BOND_PAYABLE",
        "SHORT_BOND_PAYABLE",
        "NONCURRENT_LIAB_1YEAR",
        "LEASE_LIAB",
    )
    if not set(fields).issubset(row.index):
        return None
    values = [_number(row, field) for field in fields]
    if any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]


def _retry_call(call, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return call()
        except Exception:
            if attempt >= max_retries:
                raise
            time.sleep(0.1 * (attempt + 1))
    raise RuntimeError("unreachable")


def get_market_snapshot(symbol: str, as_of: date, settings: Settings | Any) -> MarketSnapshot:
    resolved = _resolved(symbol)
    mode = getattr(settings, "fundamental_data_mode", "mock")
    if mode == "mock":
        return MarketSnapshot(
            symbol=resolved.symbol,
            as_of=as_of,
            latest_price=1_600.0,
            market_cap=2_010_000_000_000.0,
            currency="CNY",
            data_source="mock",
        )
    if mode != "live":
        raise FundamentalDataError(f"未知基本面数据模式: {mode}")
    if getattr(settings, "fundamental_data_provider", "akshare") != "akshare":
        raise FundamentalDataError("不支持的基本面数据提供方")
    try:
        return _get_live_market_snapshot(resolved.symbol, as_of, settings)
    except Exception as exc:
        if isinstance(exc, FundamentalDataError):
            raise
        raise FundamentalDataError("FINANCIAL_DATA_FAILED: 市场快照获取失败") from exc


def _get_live_market_snapshot(symbol: str, as_of: date, settings: Any) -> MarketSnapshot:
    """Live market snapshot without a token and without the flaky push2 backend.

    Two earlier attempts both produced FINANCIAL_DATA_FAILED at valuation_research:
      1. ak.stock_individual_spot_xq — Xueqiu, token-gated; upstream returns
         400016 (刷新页面或重新登录) without a login token the worker does not have.
      2. ak.stock_individual_info_em / ak.stock_zh_a_hist — auth-free, but both
         route to push2.eastmoney.com / push2his.eastmoney.com, which are
         intermittently unreachable through the worker's network path
         (requests.exceptions.ProxyError / Empty reply from server).

    This version sources market cap and the latest close from backends that share
    the reliable emweb/datacenter hosts already used by the financial sheets, with
    a second daily-kline backend as fallback for the price:
      - market cap: ak.stock_zh_scale_comparison_em (datacenter.eastmoney.com) → 总市值
      - latest close at or before as_of: ak.stock_zh_a_daily (Sina), falling back to
        ak.stock_zh_a_hist_tx (Tencent) — both token-free and reliable here.

    Date handling keys off as_of (not Date.now) so the snapshot stays deterministic
    per run. market_cap is the critical input for calculate_valuation (PE/PB/PS);
    latest_price is informational and is filtered to on-or-before as_of.
    """
    import akshare as ak

    retries = getattr(settings, "fundamental_data_max_retries", 2)
    exchange_symbol = f"{symbol[-2:]}{symbol[:6]}"        # 601899.SH -> SH601899
    quote_symbol = f"{symbol[-2:].lower()}{symbol[:6]}"   # 601899.SH -> sh601899

    # Market cap from the datacenter "company scale" endpoint — same backend
    # family as the financial sheets. The push2 individual-info endpoint is flaky
    # here, so do not fall back to it; if datacenter is down the run already cannot
    # fetch the financial sheets either, so failing fast is consistent.
    scale = _retry_call(
        lambda: ak.stock_zh_scale_comparison_em(symbol=exchange_symbol),
        retries,
    )
    if scale is None or scale.empty or "总市值" not in scale.columns:
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: AKShare 公司规模数据为空或缺少总市值")
    market_cap = _required_finite_number(scale.iloc[0]["总市值"], "总市值")

    # Latest close at or before as_of. Sina daily is primary, Tencent daily the
    # fallback; both are token-free and expose lowercase 'date'/'close' columns.
    start = as_of - timedelta(days=20)
    start_str = start.strftime("%Y%m%d")
    end_str = as_of.strftime("%Y%m%d")
    price = _latest_close_from_kline(
        _retry_call(
            lambda: ak.stock_zh_a_daily(symbol=quote_symbol, start_date=start_str, end_date=end_str, adjust=""),
            retries,
        ),
        as_of,
    )
    if price is None:
        price = _latest_close_from_kline(
            _retry_call(
                lambda: ak.stock_zh_a_hist_tx(symbol=quote_symbol, start_date=start_str, end_date=end_str, adjust=""),
                retries,
            ),
            as_of,
        )
    if price is None:
        raise FinancialDataInvalidError("FINANCIAL_DATA_INVALID: as_of 之前无可用日线行情")
    return MarketSnapshot(
        symbol=symbol,
        as_of=as_of,
        latest_price=price,
        market_cap=market_cap,
        currency="CNY",
        data_source="akshare",
    )


def _latest_close_from_kline(kline: Any, as_of: date) -> float | None:
    """Return the last close at or before as_of from a Sina/Tencent daily kline
    frame, or None if the frame is empty, lacks the expected 'date'/'close'
    columns, has no row on or before as_of, or the close is non-finite. Returning
    None lets the caller fall through to the next backend; a hard data-integrity
    failure only surfaces once every backend has been tried."""
    if kline is None:
        return None
    try:
        empty = bool(kline.empty)
    except AttributeError:
        return None
    if empty or "date" not in kline.columns or "close" not in kline.columns:
        return None
    frame = kline.copy()
    frame["date"] = frame["date"].astype(str)
    on_or_before = frame[frame["date"] <= as_of.isoformat()]
    if on_or_before.empty:
        return None
    value = pd.to_numeric(on_or_before.iloc[-1]["close"], errors="coerce")
    if pd.isna(value) or not float("-inf") < float(value) < float("inf"):
        return None
    return float(value)


def _required_finite_number(value: Any, name: str) -> float:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or not float("-inf") < float(number) < float("inf"):
        raise FinancialDataInvalidError(f"FINANCIAL_DATA_INVALID: {name}缺失或非法")
    return float(number)
