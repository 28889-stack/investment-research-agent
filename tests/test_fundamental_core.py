from __future__ import annotations

import math
from datetime import date

import pytest
import pandas as pd
from pydantic import ValidationError

from app.fundamental.data import (
    FinancialDataInvalidError,
    _get_live_market_snapshot,
    _interest_bearing_debt,
    _latest_close_from_kline,
    _map_live_company_profile,
    _map_live_financial_frames,
    get_company_profile,
    get_financial_data,
)


def test_interest_bearing_debt_preserves_missing_components_as_null() -> None:
    fields = {
        "SHORT_LOAN": 10.0,
        "LONG_LOAN": 20.0,
        "BOND_PAYABLE": 30.0,
        "SHORT_BOND_PAYABLE": 40.0,
        "NONCURRENT_LIAB_1YEAR": 50.0,
        "LEASE_LIAB": 60.0,
    }

    assert _interest_bearing_debt(pd.Series(fields)) == 210.0
    assert _interest_bearing_debt(pd.Series({**fields, "LEASE_LIAB": None})) is None
    assert _interest_bearing_debt(pd.Series({name: None for name in fields})) is None
from app.fundamental.evidence import (
    EvidenceStore,
    _BLOCKED_NETWORKS,
    is_safe_public_url,
)
from app.fundamental.financials import calculate_financial_metrics
from app.fundamental.schemas import (
    AssumptionItem,
    AssumptionStore,
    FinancialData,
    FinancialPeriod,
    MarketSnapshot,
)
from app.fundamental.valuation import calculate_valuation


def _period(
    period: str,
    *,
    revenue: float = 1_000.0,
    attributable: float = 100.0,
    ocf: float = 120.0,
    capex: float = 20.0,
    interest_bearing_debt: float | None = 200.0,
) -> FinancialPeriod:
    return FinancialPeriod(
        period=period,
        report_type="annual",
        published_date=f"{int(period[:4]) + 1}-03-20",
        revenue=revenue,
        operating_profit=150.0,
        net_profit=105.0,
        net_profit_attributable=attributable,
        total_assets=2_000.0,
        total_liabilities=600.0,
        interest_bearing_debt=interest_bearing_debt,
        shareholders_equity=1_400.0,
        current_assets=900.0,
        current_liabilities=300.0,
        cash=500.0,
        accounts_receivable=50.0,
        inventory=100.0,
        operating_cash_flow=ocf,
        capital_expenditure=capex,
        basic_eps=1.0,
        shares_outstanding=100.0,
    )


def _financial_data(*periods: FinancialPeriod) -> FinancialData:
    return FinancialData(
        symbol="600519.SH",
        as_of="2026-08-05",
        currency="CNY",
        unit="CNY",
        data_source="mock",
        periods=list(periods),
    )


def test_mock_company_and_five_year_financial_data(settings) -> None:
    profile = get_company_profile("600519.SH", date(2026, 8, 5), settings)
    data = get_financial_data("600519.SH", date(2026, 8, 5), settings)

    assert profile.short_name == "贵州茅台"
    assert profile.data_source == "mock"
    assert data.symbol == "600519.SH"
    assert len(data.periods) >= 5
    assert data.periods == sorted(data.periods, key=lambda item: item.period)
    assert all(item.published_date <= data.as_of.isoformat() for item in data.periods)


def test_financial_data_rejects_symbol_mismatch_duplicate_period_and_non_finite() -> None:
    first = _period("2024-12-31")
    with pytest.raises(ValidationError, match="period"):
        _financial_data(first, first)
    with pytest.raises(ValidationError, match="finite"):
        _financial_data(_period("2024-12-31", revenue=math.inf))
    with pytest.raises(ValueError, match="symbol"):
        get_financial_data("AAPL", date(2026, 8, 5), type("S", (), {"fundamental_data_mode": "mock"})())


def test_live_financial_mapping_filters_reports_published_after_as_of() -> None:
    common = {
        "REPORT_DATE": [f"{year}-12-31" for year in range(2020, 2026)],
        "NOTICE_DATE": [f"{year + 1}-03-20" for year in range(2020, 2025)] + ["2026-04-17"],
        "REPORT_TYPE": ["年报"] * 6,
        "CURRENCY": ["CNY"] * 6,
    }
    sequence = [float(value) for value in range(100, 106)]
    profit = pd.DataFrame({**common, "TOTAL_OPERATE_INCOME": sequence, "OPERATE_PROFIT": [20.0] * 6, "NETPROFIT": [15.0] * 6, "PARENT_NETPROFIT": [14.0] * 6, "BASIC_EPS": [1.4] * 6})
    balance = pd.DataFrame({**common, "TOTAL_ASSETS": [200.0] * 6, "TOTAL_LIABILITIES": [60.0] * 6, "TOTAL_PARENT_EQUITY": [140.0] * 6, "TOTAL_CURRENT_ASSETS": [100.0] * 6, "TOTAL_CURRENT_LIAB": [30.0] * 6, "MONETARYFUNDS": [50.0] * 6, "ACCOUNTS_RECE": [5.0] * 6, "INVENTORY": [10.0] * 6, "SHARE_CAPITAL": [10.0] * 6})
    cash = pd.DataFrame({**common, "NETCASH_OPERATE": [18.0] * 6, "CONSTRUCT_LONG_ASSET": [3.0] * 6})

    result = _map_live_financial_frames("600519.SH", date(2026, 3, 31), balance, profit, cash)

    assert [item.period for item in result.periods] == [f"{year}-12-31" for year in range(2020, 2025)]
    assert result.periods[0].revenue == 100.0
    assert result.periods[0].capital_expenditure == 3.0
    assert result.data_source == "akshare"


def test_live_financial_mapping_rejects_fewer_than_five_annual_periods() -> None:
    common = {
        "REPORT_DATE": ["2024-12-31"],
        "NOTICE_DATE": ["2025-03-20"],
        "REPORT_TYPE": ["年报"],
        "CURRENCY": ["CNY"],
    }
    profit = pd.DataFrame({**common, "TOTAL_OPERATE_INCOME": [100.0]})
    balance = pd.DataFrame({**common, "TOTAL_ASSETS": [200.0]})
    cash = pd.DataFrame({**common, "NETCASH_OPERATE": [18.0]})

    with pytest.raises(FinancialDataInvalidError, match="5") as error:
        _map_live_financial_frames("600519.SH", date(2026, 3, 31), balance, profit, cash)
    assert error.value.code == "FINANCIAL_DATA_INVALID"


def test_live_company_profile_maps_cninfo_columns() -> None:
    frame = pd.DataFrame(
        [
            {
                "公司名称": "贵州茅台酒股份有限公司",
                "A股简称": "贵州茅台",
                "所属行业": "酒、饮料和精制茶制造业",
                "上市日期": "2001-08-27",
                "主营业务": "茅台酒系列产品的生产与销售。",
            }
        ]
    )

    result = _map_live_company_profile("600519.SH", date(2026, 8, 5), frame)

    assert result.company_name == "贵州茅台酒股份有限公司"
    assert result.short_name == "贵州茅台"
    assert result.industry == "酒、饮料和精制茶制造业"
    assert result.data_source == "akshare"


def test_live_market_snapshot_combines_info_and_kline(monkeypatch) -> None:
    """Live snapshot no longer uses the token-gated Xueqiu spot endpoint (which
    raises 400016 刷新页面或重新登录 without a token, the original root cause of
    FINANCIAL_DATA_FAILED at valuation_research) nor the flaky push2/push2his
    Eastmoney backends (intermittent ProxyError / Empty reply from server). It
    now sources market cap from stock_zh_scale_comparison_em (datacenter.eastmoney,
    same reliable host as the financial sheets) and the latest close from
    stock_zh_a_daily (Sina), falling back to stock_zh_a_hist_tx (Tencent). The
    price is the last close on or before as_of."""
    import sys
    from types import SimpleNamespace

    scale = pd.DataFrame(
        [{"代码": "601899", "简称": "紫金矿业", "总市值": 945_831_719_104.54}]
    )
    sina = pd.DataFrame(
        {
            "date": ["2026-08-04", "2026-08-05", "2026-08-06"],
            "close": [32.12, 34.10, 34.50],
        }
    )

    calls = {"tx": 0}

    def fake_tx(symbol, start_date, end_date, adjust, timeout=None):
        calls["tx"] += 1
        return pd.DataFrame()

    fake = SimpleNamespace(
        stock_zh_scale_comparison_em=lambda symbol: scale,
        stock_zh_a_daily=lambda symbol, start_date, end_date, adjust: sina,
        stock_zh_a_hist_tx=fake_tx,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    from app.config import Settings

    settings = Settings.from_env()
    result = _get_live_market_snapshot("601899.SH", date(2026, 8, 6), settings)

    assert result.latest_price == pytest.approx(34.50)
    assert result.market_cap == pytest.approx(945_831_719_104.54)
    assert result.currency == "CNY"
    assert result.data_source == "akshare"
    assert calls["tx"] == 0, "Tencent fallback must not run when Sina succeeds"


def test_live_market_snapshot_falls_back_to_tencent_kline(monkeypatch) -> None:
    """When Sina returns an empty frame, the snapshot falls back to the Tencent
    daily kline (stock_zh_a_hist_tx) for the close at or before as_of, rather
    than failing — both backends are token-free and reliable."""
    import sys
    from types import SimpleNamespace

    scale = pd.DataFrame([{"代码": "601899", "简称": "紫金矿业", "总市值": 945_000_000_000.0}])
    tx = pd.DataFrame(
        {
            "date": ["2026-08-04", "2026-08-05", "2026-08-06"],
            "close": [32.12, 34.10, 34.50],
        }
    )

    fake = SimpleNamespace(
        stock_zh_scale_comparison_em=lambda symbol: scale,
        stock_zh_a_daily=lambda symbol, start_date, end_date, adjust: pd.DataFrame(),
        stock_zh_a_hist_tx=lambda symbol, start_date, end_date, adjust, timeout=None: tx,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    from app.config import Settings

    settings = Settings.from_env()
    result = _get_live_market_snapshot("601899.SH", date(2026, 8, 6), settings)

    assert result.latest_price == pytest.approx(34.50)
    assert result.market_cap == pytest.approx(945_000_000_000.0)


def test_live_market_snapshot_rejects_when_all_klines_empty(monkeypatch) -> None:
    """If every kline backend returns a frame with no row on or before as_of
    (e.g. as_of predates all available rows), the snapshot fails with an explicit
    FINANCIAL_DATA_INVALID rather than silently using a future close or a None price."""
    import sys
    from types import SimpleNamespace

    scale = pd.DataFrame([{"代码": "601899", "简称": "紫金矿业", "总市值": 945_000_000_000.0}])
    # All rows are strictly after as_of (2026-08-04), so on_or_before is empty.
    future_only = pd.DataFrame({"date": ["2026-08-05", "2026-08-06"], "close": [34.10, 34.50]})

    fake = SimpleNamespace(
        stock_zh_scale_comparison_em=lambda symbol: scale,
        stock_zh_a_daily=lambda symbol, start_date, end_date, adjust: future_only,
        stock_zh_a_hist_tx=lambda symbol, start_date, end_date, adjust, timeout=None: future_only,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    from app.config import Settings

    settings = Settings.from_env()
    with pytest.raises(FinancialDataInvalidError, match="as_of"):
        _get_live_market_snapshot("601899.SH", date(2026, 8, 4), settings)


def test_latest_close_from_kline_filters_on_or_before_as_of() -> None:
    """The shared helper returns the last close <= as_of and tolerates the edge
    cases the snapshot path depends on: empty/None frame -> None, missing columns
    -> None, non-finite close -> None."""
    assert _latest_close_from_kline(None, date(2026, 8, 6)) is None
    assert _latest_close_from_kline(pd.DataFrame(), date(2026, 8, 6)) is None
    assert _latest_close_from_kline(pd.DataFrame({"date": ["2026-08-05"]}), date(2026, 8, 6)) is None

    frame = pd.DataFrame(
        {"date": ["2026-08-04", "2026-08-05", "2026-08-06"], "close": [32.12, 34.10, 34.50]}
    )
    assert _latest_close_from_kline(frame, date(2026, 8, 6)) == pytest.approx(34.50)
    assert _latest_close_from_kline(frame, date(2026, 8, 5)) == pytest.approx(34.10)
    assert _latest_close_from_kline(frame, date(2026, 8, 3)) is None

    bad = pd.DataFrame({"date": ["2026-08-05"], "close": [float("nan")]})
    assert _latest_close_from_kline(bad, date(2026, 8, 6)) is None


def test_financial_metrics_use_deterministic_formulas() -> None:
    data = _financial_data(
        _period("2023-12-31", revenue=800.0, attributable=80.0, ocf=90.0, capex=15.0),
        _period("2024-12-31", revenue=1_000.0, attributable=100.0, ocf=120.0, capex=20.0),
    )
    metrics = calculate_financial_metrics(data, "financial_metric_v1")

    assert metrics.growth["2024-12-31"]["revenue_yoy"] == pytest.approx(0.25)
    assert metrics.growth["2024-12-31"]["net_profit_attributable_yoy"] == pytest.approx(0.25)
    assert metrics.profitability["2024-12-31"]["net_margin"] == pytest.approx(0.105)
    assert metrics.profitability["2024-12-31"]["roa"] == pytest.approx(0.0525)
    assert metrics.profitability["2024-12-31"]["roe"] == pytest.approx(0.075)
    assert metrics.balance_sheet["2024-12-31"]["debt_to_assets"] == pytest.approx(0.3)
    assert metrics.balance_sheet["2024-12-31"]["current_ratio"] == pytest.approx(3.0)
    assert metrics.cash_flow["2024-12-31"]["ocf_to_net_profit"] == pytest.approx(120 / 105)
    assert metrics.cash_flow["2024-12-31"]["free_cash_flow"] == pytest.approx(100.0)


def test_evidence_store_assigns_unique_ids_and_rejects_unknown_refs(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.json")
    first = store.add(
        claim="营业收入历史事实",
        content="年报摘要",
        source_name="2024 年年度报告",
        url="https://example.com/report",
        date_value="2025-03-20",
        location="第 10 页",
        evidence_type="historical_fact",
    )
    second = store.add(
        claim="管理层表述",
        content="经营计划摘要",
        source_name="股东大会",
        url="https://example.com/meeting",
        date_value="2025-05-01",
        location="",
        evidence_type="management_statement",
    )

    assert first.id == "ev_001"
    assert second.id == "ev_002"
    assert EvidenceStore(tmp_path / "evidence.json").load().items[1].id == "ev_002"
    store.validate_ids(["ev_001", "ev_002"])
    with pytest.raises(ValueError, match="Evidence"):
        store.validate_ids(["ev_999"])


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://172.16.2.3/a",
        "http://192.168.1.1/a",
        "http://169.254.1.1/a",
        "http://0.0.0.0/a",
        "file:///etc/passwd",
    ],
)
def test_research_source_blocks_local_and_private_urls(url: str) -> None:
    assert is_safe_public_url(url) is False


def test_research_source_fails_closed_when_dns_resolution_fails(monkeypatch) -> None:
    import socket

    def fail(*_args, **_kwargs):
        raise socket.gaierror("unresolvable")

    monkeypatch.setattr("app.fundamental.evidence.socket.getaddrinfo", fail)

    assert is_safe_public_url("https://unresolvable.example/report") is False


def test_research_source_blacklist_excludes_fake_ip_proxy_range() -> None:
    """198.18.0.0/15 is the local Fake-IP proxy range that actually routes to
    the public internet via the system proxy. It must NOT be in the blacklist,
    and it must not be caught by ipaddress.is_private-based filtering."""
    blocked = {str(net) for net in _BLOCKED_NETWORKS}
    assert not any(net.startswith("198.18") for net in blocked)

    import ipaddress

    # Sanity: the proxy range IS is_private=True, which is exactly why the
    # blacklist uses explicit segments instead of is_private filtering.
    assert ipaddress.ip_address("198.18.0.1").is_private is True


def test_relative_valuation_and_dcf_are_calculated_by_python() -> None:
    data = _financial_data(_period("2024-12-31"))
    metrics = calculate_financial_metrics(data, "financial_metric_v1")
    assumptions = AssumptionStore(
        items=[
            AssumptionItem(id="asm_001", variable="fcf_growth", value=0.08, period="FY2025-FY2029", source="financial_research", owner="financial_research"),
            AssumptionItem(id="asm_002", variable="terminal_growth", value=0.03, period="terminal", source="financial_research", owner="financial_research"),
            AssumptionItem(id="asm_003", variable="discount_rate", value=0.10, period="forecast", source="financial_research", owner="financial_research"),
        ]
    )
    snapshot = MarketSnapshot(symbol="600519.SH", as_of="2026-08-05", latest_price=20.0, market_cap=2_000.0, currency="CNY", data_source="mock")

    result = calculate_valuation(data, metrics, assumptions, snapshot, "valuation_v1")

    assert result.relative.pe.status == "available"
    assert result.relative.pe.value == pytest.approx(20.0)
    assert result.relative.pb.value == pytest.approx(2_000 / 1_400)
    assert result.relative.ps.value == pytest.approx(2.0)
    assert result.dcf.status == "available"
    assert result.dcf.per_share_value is not None
    assert result.dcf.valuation_range[0] <= result.dcf.per_share_value <= result.dcf.valuation_range[1]
    assert set(result.dcf.sensitivity) == {"low_growth", "base", "high_growth"}
    assert result.assumption_ids == ["asm_001", "asm_002", "asm_003"]


def test_valuation_marks_zero_denominator_and_missing_fcf_unavailable() -> None:
    period = _period("2024-12-31", revenue=0.0, attributable=0.0, ocf=10.0, capex=10.0)
    period.shareholders_equity = 0.0
    data = _financial_data(period)
    metrics = calculate_financial_metrics(data, "financial_metric_v1")
    result = calculate_valuation(
        data,
        metrics,
        AssumptionStore(items=[]),
        MarketSnapshot(symbol="600519.SH", as_of="2026-08-05", latest_price=1.0, market_cap=100.0, currency="CNY", data_source="mock"),
        "valuation_v1",
    )

    assert result.relative.pe.status == "unavailable"
    assert result.relative.pb.status == "unavailable"
    assert result.relative.ps.status == "unavailable"
    assert result.dcf.status == "unavailable"


def test_dcf_requires_cash_and_interest_bearing_debt() -> None:
    assumptions = AssumptionStore(
        items=[
            AssumptionItem(id="asm_001", variable="fcf_growth", value=0.08, period="forecast", source="financial_research", owner="financial_research"),
            AssumptionItem(id="asm_002", variable="terminal_growth", value=0.03, period="terminal", source="financial_research", owner="financial_research"),
            AssumptionItem(id="asm_003", variable="discount_rate", value=0.10, period="forecast", source="financial_research", owner="financial_research"),
        ]
    )
    for period in (
        _period("2024-12-31", interest_bearing_debt=None),
        _period("2024-12-31"),
    ):
        if period.interest_bearing_debt is not None:
            period.cash = None
        data = _financial_data(period)
        result = calculate_valuation(
            data,
            calculate_financial_metrics(data, "financial_metric_v1"),
            assumptions,
            MarketSnapshot(symbol="600519.SH", as_of="2026-08-05", latest_price=20.0, market_cap=2_000.0, currency="CNY", data_source="mock"),
            "valuation_v1",
        )
        assert result.dcf.status == "unavailable"


def test_valuation_rejects_market_snapshot_as_of_and_currency_mismatch() -> None:
    data = _financial_data(_period("2024-12-31"))
    metrics = calculate_financial_metrics(data, "financial_metric_v1")
    assumptions = AssumptionStore(items=[])

    with pytest.raises(ValueError, match="as_of"):
        calculate_valuation(
            data,
            metrics,
            assumptions,
            MarketSnapshot(symbol="600519.SH", as_of="2026-08-04", latest_price=20.0, market_cap=2_000.0, currency="CNY", data_source="mock"),
            "valuation_v1",
        )
    with pytest.raises(ValueError, match="currency"):
        calculate_valuation(
            data,
            metrics,
            assumptions,
            MarketSnapshot(symbol="600519.SH", as_of="2026-08-05", latest_price=20.0, market_cap=2_000.0, currency="USD", data_source="mock"),
            "valuation_v1",
        )
