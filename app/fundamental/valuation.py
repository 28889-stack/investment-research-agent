from __future__ import annotations

from app.fundamental.schemas import (
    AssumptionStore,
    DcfResult,
    FinancialData,
    FinancialMetrics,
    MarketSnapshot,
    RelativeMethod,
    RelativeValuation,
    ValuationResult,
)


def _relative(market_cap: float | None, denominator: float | None, name: str) -> RelativeMethod:
    if market_cap is None or denominator in {None, 0}:
        return RelativeMethod(status="unavailable", value=None, reason=f"{name} 必需输入缺失或为零")
    return RelativeMethod(status="available", value=market_cap / denominator)


def calculate_valuation(
    financial_data: FinancialData,
    financial_metrics: FinancialMetrics,
    assumptions: AssumptionStore,
    market_snapshot: MarketSnapshot,
    script_version: str,
) -> ValuationResult:
    if financial_data.symbol != market_snapshot.symbol or financial_metrics.symbol != financial_data.symbol:
        raise ValueError("估值输入 symbol 不一致")
    if market_snapshot.as_of != financial_data.as_of or financial_metrics.as_of != financial_data.as_of:
        raise ValueError("估值输入 as_of 不一致")
    if market_snapshot.currency != financial_data.currency:
        raise ValueError("估值输入 currency 不一致")
    latest = financial_data.periods[-1]
    relative = RelativeValuation(
        pe=_relative(market_snapshot.market_cap, latest.net_profit_attributable, "PE"),
        pb=_relative(market_snapshot.market_cap, latest.shareholders_equity, "PB"),
        ps=_relative(market_snapshot.market_cap, latest.revenue, "PS"),
    )
    values = {item.variable: item.value for item in assumptions.items}
    assumption_ids = [item.id for item in assumptions.items]
    free_cash_flow = financial_metrics.cash_flow[latest.period]["free_cash_flow"]
    required = {"fcf_growth", "terminal_growth", "discount_rate"}
    if (
        free_cash_flow in {None, 0}
        or latest.shares_outstanding in {None, 0}
        or latest.cash is None
        or latest.interest_bearing_debt is None
        or not required.issubset(values)
        or values.get("fcf_growth", -1) <= -1
        or values.get("terminal_growth", -1) <= -1
        or values.get("discount_rate", 0) <= 0
        or values.get("discount_rate", 0) <= values.get("terminal_growth", 0)
    ):
        dcf = DcfResult(
            status="unavailable",
            per_share_value=None,
            valuation_range=None,
            sensitivity={},
            reason="DCF 必需输入缺失或无效",
        )
    else:
        sensitivity = {
            "low_growth": _dcf_per_share(free_cash_flow, values["fcf_growth"] - 0.02, values["terminal_growth"], values["discount_rate"], latest.cash, latest.interest_bearing_debt, latest.shares_outstanding),
            "base": _dcf_per_share(free_cash_flow, values["fcf_growth"], values["terminal_growth"], values["discount_rate"], latest.cash, latest.interest_bearing_debt, latest.shares_outstanding),
            "high_growth": _dcf_per_share(free_cash_flow, values["fcf_growth"] + 0.02, values["terminal_growth"], values["discount_rate"], latest.cash, latest.interest_bearing_debt, latest.shares_outstanding),
        }
        ordered = sorted(sensitivity.values())
        dcf = DcfResult(
            status="available",
            per_share_value=sensitivity["base"],
            valuation_range=(ordered[0], ordered[-1]),
            sensitivity=sensitivity,
        )
    return ValuationResult(
        symbol=financial_data.symbol,
        as_of=financial_data.as_of,
        script_version=script_version,
        relative=relative,
        dcf=dcf,
        assumption_ids=assumption_ids,
        market_snapshot=market_snapshot,
    )


def _dcf_per_share(
    starting_fcf: float,
    growth: float,
    terminal_growth: float,
    discount_rate: float,
    cash: float,
    interest_bearing_debt: float,
    shares: float,
) -> float:
    present_value = 0.0
    projected = starting_fcf
    for year in range(1, 6):
        projected *= 1 + growth
        present_value += projected / ((1 + discount_rate) ** year)
    terminal = projected * (1 + terminal_growth) / (discount_rate - terminal_growth)
    enterprise_value = present_value + terminal / ((1 + discount_rate) ** 5)
    equity_value = enterprise_value + cash - interest_bearing_debt
    return equity_value / shares
