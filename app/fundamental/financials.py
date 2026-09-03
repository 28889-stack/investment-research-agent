from __future__ import annotations

from app.fundamental.schemas import FinancialData, FinancialMetrics


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in {None, 0}:
        return None
    return numerator / denominator


def calculate_financial_metrics(data: FinancialData, script_version: str) -> FinancialMetrics:
    growth = {}
    profitability = {}
    balance_sheet = {}
    cash_flow = {}
    efficiency = {}
    missing: list[str] = []
    previous = None
    for item in data.periods:
        period = item.period
        growth[period] = {
            "revenue_yoy": _growth(item.revenue, previous.revenue if previous else None),
            "net_profit_attributable_yoy": _growth(item.net_profit_attributable, previous.net_profit_attributable if previous else None),
            "operating_cash_flow_yoy": _growth(item.operating_cash_flow, previous.operating_cash_flow if previous else None),
        }
        profitability[period] = {
            "operating_margin": _ratio(item.operating_profit, item.revenue),
            "net_margin": _ratio(item.net_profit, item.revenue),
            "roa": _ratio(item.net_profit, item.total_assets),
            "roe": _ratio(item.net_profit, item.shareholders_equity),
        }
        balance_sheet[period] = {
            "debt_to_assets": _ratio(item.total_liabilities, item.total_assets),
            "current_ratio": _ratio(item.current_assets, item.current_liabilities),
            "cash_to_assets": _ratio(item.cash, item.total_assets),
        }
        free_cash_flow = (
            item.operating_cash_flow - item.capital_expenditure
            if item.operating_cash_flow is not None and item.capital_expenditure is not None
            else None
        )
        cash_flow[period] = {
            "ocf_to_net_profit": _ratio(item.operating_cash_flow, item.net_profit),
            "free_cash_flow": free_cash_flow,
            "free_cash_flow_margin": _ratio(free_cash_flow, item.revenue),
        }
        efficiency[period] = {
            "receivables_to_revenue": _ratio(item.accounts_receivable, item.revenue),
            "inventory_to_revenue": _ratio(item.inventory, item.revenue),
            "asset_turnover": _ratio(item.revenue, item.total_assets),
        }
        for group_name, group in (
            ("growth", growth[period]),
            ("profitability", profitability[period]),
            ("balance_sheet", balance_sheet[period]),
            ("cash_flow", cash_flow[period]),
            ("efficiency", efficiency[period]),
        ):
            missing.extend(f"{period}.{group_name}.{key}" for key, value in group.items() if value is None)
        previous = item
    return FinancialMetrics(
        symbol=data.symbol,
        as_of=data.as_of,
        script_version=script_version,
        periods=[item.period for item in data.periods],
        growth=growth,
        profitability=profitability,
        balance_sheet=balance_sheet,
        cash_flow=cash_flow,
        efficiency=efficiency,
        missing_metrics=missing,
    )


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in {None, 0}:
        return None
    return current / previous - 1
