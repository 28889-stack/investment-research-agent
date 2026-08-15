from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.runtime.exceptions import (
    AgentTimeoutError,
    BridgeCrashedError,
    BridgeProtocolError,
    BridgeStartError,
    ToolBudgetExhaustedError,
    ToolNotAllowedError,
)
from app.runtime.security import safe_error_message


ToolCallHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class PiClient(Protocol):
    def health_check(self) -> dict[str, Any]: ...

    def create_session(
        self,
        *,
        session_id: str,
        profile: dict[str, Any],
        model: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> None: ...

    def run_agent(
        self,
        *,
        session_id: str,
        system_prompt: str,
        context: dict[str, Any],
        task: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
        tool_handler: ToolCallHandler,
    ) -> str: ...

    def repair_output(
        self,
        *,
        session_id: str,
        raw_output: str,
        validation_error: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> str: ...

    def close_session(self, session_id: str) -> None: ...

    def shutdown(self) -> None: ...


def _valid_output(task_id: str, summary: str, findings: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "status": "completed",
            "summary": summary,
            "findings": findings,
            "new_evidence": [],
            "new_assumptions": [],
            "risks": [],
            "conflicts": [],
            "missing_information": [],
            "suggested_followups": [],
        },
        ensure_ascii=False,
    )


class MockPiClient:
    """Deterministic test double. It never performs network or model calls."""

    def __init__(self, scenario: str = "valid") -> None:
        self.scenario = scenario
        self.sessions: dict[str, dict[str, Any]] = {}
        self.closed_sessions: list[str] = []

    def health_check(self) -> dict[str, Any]:
        if self.scenario == "bridge_crash":
            raise BridgeCrashedError("Mock Bridge 已退出")
        return {"status": "ok", "mode": "mock"}

    def create_session(
        self,
        *,
        session_id: str,
        profile: dict[str, Any],
        model: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> None:
        if session_id in self.sessions:
            raise ValueError(f"Session already exists: {session_id}")
        self.sessions[session_id] = {
            "profile": profile,
            "model": model,
            "tools": tools,
        }

    def run_agent(
        self,
        *,
        session_id: str,
        system_prompt: str,
        context: dict[str, Any],
        task: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
        tool_handler: ToolCallHandler,
    ) -> str:
        del system_prompt, task, output_schema, timeout_seconds
        session = self.sessions.get(session_id)
        if session is None:
            raise BridgeCrashedError("Mock Session 不存在")
        if self.scenario == "timeout":
            raise AgentTimeoutError("Mock Agent 超时")
        if self.scenario == "bridge_crash":
            raise BridgeCrashedError("Mock Bridge 已退出")
        if self.scenario == "unauthorized_tool":
            tool_handler("shell", {"command": "echo unsafe"})
        if self.scenario == "invalid_json":
            return "this is not json"
        if self.scenario == "schema_failure":
            return '{"task_id":"bad"}'

        profile = session["profile"]
        if profile["profile_id"] == "technical_research":
            market = tool_handler("get_market_data", {})
            tool_handler("calculate_technical_indicators", {})
            summary = tool_handler("get_technical_summary", {})
            alignment = summary["trend"]["alignment"]
            conflicts = []
            if alignment == "bullish" and summary["macd"]["cross"] == "bearish":
                conflicts.append("均线趋势偏强，但 MACD 当前状态偏弱")
            return json.dumps(
                {
                    "symbol": market["symbol"],
                    "as_of": market["as_of"],
                    "data_version": market["data_version"],
                    "trend": f"均线排列状态为 {alignment}，趋势信号由指标脚本确认。",
                    "volume_price": "成交量与价格关系以脚本输出的均量信号为依据。",
                    "momentum": "MACD、RSI 与 KDJ 信号存在不同观察周期。",
                    "volatility": "历史波动率和 ATR 反映近期波动区间。",
                    "support_resistance": "支撑与阻力采用近期和中期价格区间极值。",
                    "patterns": summary["patterns"],
                    "short_term": "短期关注动量信号及近期价格区间。",
                    "medium_term": "中期关注中期与长期均线的相对关系。",
                    "long_term": "长期结论受当前历史窗口限制，需要持续观察。",
                    "conflicts": conflicts,
                    "risks": ["技术指标存在滞后性", "历史行情不能保证未来表现"],
                    "confidence": "medium",
                },
                ensure_ascii=False,
            )
        if profile["profile_id"] == "technical_assembly":
            research = context["technical_research"]
            kronos = context["kronos"]
            probabilities = kronos["direction_probability"]
            dominant = max(probabilities, key=probabilities.get)
            conflicts = list(research["conflicts"])
            if dominant == "flat" and "偏强" in research["medium_term"]:
                conflicts.append("技术指标中期偏强，而 Kronos 方向概率以震荡为主")
            return json.dumps(
                {
                    "symbol": research["symbol"],
                    "as_of": research["as_of"],
                    "data_version": research["data_version"],
                    "summary": "技术指标解释与 Kronos 预测已按同一数据版本完成对比。",
                    "agreements": ["两类结果均基于同一标准化历史行情"],
                    "conflicts": conflicts,
                    "uncertainties": ["模型概率与技术指标均不能消除未来不确定性"],
                    "short_term": research["short_term"],
                    "medium_term": research["medium_term"],
                    "long_term": research["long_term"],
                    "risks": research["risks"],
                    "conclusion": "当前信号需结合冲突与不确定性审慎理解。",
                    "disclaimer": "本输出不构成投资建议或交易指令。",
                },
                ensure_ascii=False,
            )
        node = str(context.get("node", ""))
        run = context.get("run", {})
        symbol = str(run.get("resolved_symbol") or run.get("symbol") or "")
        as_of = str(run.get("as_of") or "")
        if profile["profile_id"] == "fundamental_lead" and node == "lead_planning":
            tool_handler("get_company_profile", {})
            sources = tool_handler("search_research_sources", {"query": "公司业务与年报"})
            evidence = tool_handler(
                "read_research_source",
                {
                    "result_id": sources["items"][0]["result_id"],
                    "claim": "公司主要业务与经营特征",
                    "evidence_type": "historical_fact",
                },
            )
            return json.dumps(
                {
                    "symbol": symbol,
                    "as_of": as_of,
                    "thesis": "研究主线聚焦品牌壁垒、行业需求、现金流与估值假设。",
                    "key_questions": ["需求和渠道的可持续性如何"],
                    "business_scope": ["产品结构", "渠道与品牌壁垒"],
                    "industry_scope": ["供需和竞争格局"],
                    "financial_focus": ["增长", "盈利能力", "自由现金流"],
                    "valuation_focus": ["相对估值", "简化 DCF"],
                    "risks_to_verify": ["行业需求波动", "估值假设敏感性"],
                    "evidence_ids": [evidence["evidence_id"]],
                },
                ensure_ascii=False,
            )
        if profile["profile_id"] == "business_research":
            tool_handler("get_company_profile", {})
            sources = tool_handler("search_research_sources", {"query": "商业模式产品渠道"})
            evidence = tool_handler("read_research_source", {"result_id": sources["items"][0]["result_id"], "claim": "公司商业模式和渠道", "evidence_type": "historical_fact"})
            return json.dumps({"symbol": symbol, "summary": "公司业务聚焦核心品牌与酒类产品。", "findings": [{"claim": "品牌与渠道是商业模式的关键环节。", "evidence_ids": [evidence["evidence_id"]], "confidence": "medium"}], "risks": ["需求与渠道变化风险"], "missing_information": []}, ensure_ascii=False)
        if profile["profile_id"] == "industry_research":
            sources = tool_handler("search_research_sources", {"query": "白酒行业供需与竞争"})
            selected = sources["items"][1] if len(sources["items"]) > 1 else sources["items"][0]
            evidence = tool_handler("read_research_source", {"result_id": selected["result_id"], "claim": "行业供需与竞争格局", "evidence_type": "third_party_forecast"})
            missing = ["缺少统一可比公司数据"] if self.scenario == "lead_not_ready" else []
            return json.dumps({"symbol": symbol, "summary": "行业需求具有周期性，头部品牌优势与竞争并存。", "findings": [{"claim": "行业竞争聚焦品牌和渠道。", "evidence_ids": [evidence["evidence_id"]], "confidence": "medium"}], "risks": ["周期与政策变化风险"], "missing_information": missing}, ensure_ascii=False)
        if profile["profile_id"] == "fundamental_lead" and node == "lead_review":
            artifacts = context["artifacts"]
            missing = list(artifacts["business_research"]["missing_information"]) + list(artifacts["industry_research"]["missing_information"])
            return json.dumps({"symbol": symbol, "business_status": "accepted", "industry_status": "accepted_with_gaps" if missing else "accepted", "key_findings": ["品牌渠道优势与行业周期风险并存"], "conflicts": ["业务稳定性与行业周期波动需继续验证"], "financial_questions": ["自由现金流能否支持长期估值"], "missing_information": missing, "followup_research_tasks": ["补充核验自由现金流与资本开支的关系"], "deep_research_tasks": [{"task_id": "deep_01", "topic": "自由现金流与资本开支", "scope": "核验现金流质量及其对估值假设的影响", "research_questions": ["自由现金流能否覆盖资本开支并支持长期估值"], "priority_fact_types": ["historical_fact"], "known_material": ["品牌渠道优势与行业周期风险并存"], "excluded_claims": ["业务稳定性与行业周期波动需继续验证"]}]}, ensure_ascii=False)
        if profile["profile_id"] == "deep_research":
            artifacts = context["artifacts"]
            missing = list(artifacts["lead_review"]["missing_information"])
            cards = artifacts["lead_review"].get("deep_research_tasks", [])
            task_card_id = cards[0]["task_id"] if cards else "deep_01"
            sources = tool_handler("search_research_sources", {"query": "补充核验 Lead Review 缺失项", "task_card_id": task_card_id})
            evidence = tool_handler("read_research_source", {"result_id": sources["items"][0]["result_id"], "claim": "补充检索对 Lead 缺失项的核验", "evidence_type": "historical_fact"})
            topics = [{"task_id": card["task_id"], "topic": card["topic"], "summary": "已围绕该专题完成一轮来源核验，并保留待补充的口径。", "findings": [{"claim": "补充检索对核心缺失项进行了来源核验。", "evidence_ids": [evidence["evidence_id"]], "confidence": "medium"}], "risks": [], "missing_information": []} for card in cards]
            return json.dumps({"symbol": symbol, "summary": "已根据 Lead Review 的专题任务卡完成补充检索。", "findings": [{"claim": "补充检索对核心缺失项进行了来源核验。", "evidence_ids": [evidence["evidence_id"]], "confidence": "medium"}], "risks": [], "missing_information": missing, "topics": topics}, ensure_ascii=False)
        if profile["profile_id"] == "financial_research":
            return json.dumps({"symbol": symbol, "summary": "历史财务数据显示盈利与现金流需结合增长假设解读。", "growth_analysis": "关注收入与归母净利涨幅的匹配。", "profitability_analysis": "关注利润率和净资产收益率的持续性。", "cash_flow_analysis": "经营现金流与自由现金流是估值的关键输入。", "balance_sheet_analysis": "现金与负债结构影响股权价值。", "earnings_drivers": ["产品结构", "渠道效率", "需求变化"], "assumptions": [{"variable": "fcf_growth", "value": 0.08, "period": "FY2026-FY2030", "source": "financial_research"}, {"variable": "terminal_growth", "value": 0.03, "period": "terminal", "source": "financial_research"}, {"variable": "discount_rate", "value": 0.10, "period": "forecast", "source": "financial_research"}], "risks": ["假设对估值结果敏感"], "evidence_ids": ["ev_001"], "confidence": "medium"}, ensure_ascii=False)
        if profile["profile_id"] == "valuation_research":
            assumptions = context["artifacts"]["assumptions"]["items"]
            valuation = context["artifacts"]["valuation_result"]
            methods = [name.upper() for name, item in valuation["relative"].items() if item["status"] == "available"]
            if valuation["dcf"]["status"] == "available":
                methods.append("DCF")
            return json.dumps({"symbol": symbol, "summary": "相对估值与 DCF 已由 Python 脚本计算。", "methods_used": methods, "interpretation": "估值结果应与业务质量和数据局限一并理解。", "sensitivity": "增长假设变化会改变 DCF 结果。", "risks": ["市场价格和关键假设可变"], "assumption_ids": [item["id"] for item in assumptions], "evidence_ids": ["ev_001"], "confidence": "medium"}, ensure_ascii=False)
        if profile["profile_id"] == "fundamental_lead" and node == "lead_final_review":
            artifacts = context["artifacts"]
            missing = list(dict.fromkeys(artifacts["lead_review"]["missing_information"] + artifacts["deep_research"]["missing_information"]))
            return json.dumps({"symbol": symbol, "research_thesis": artifacts["lead_plan"]["thesis"], "approved_sections": ["business", "industry", "financial", "valuation"], "key_findings": artifacts["lead_review"]["key_findings"], "conflicts": artifacts["lead_review"]["conflicts"], "missing_information": missing, "report_outline": ["公司业务", "行业", "财务", "估值", "风险"], "ready_for_writer": not missing}, ensure_ascii=False)
        if profile["profile_id"] == "lead_synthesis":
            artifacts = context["artifacts"]
            sections = [
                {"section": "business", "main_point": artifacts["business_research"]["summary"], "material_usage": "采用业务研究简报中已引用的来源。", "allowed_evidence_ids": artifacts["business_research"]["findings"][0]["evidence_ids"], "allowed_assumption_ids": []},
                {"section": "industry", "main_point": artifacts["industry_research"]["summary"], "material_usage": "采用行业研究简报中已引用的来源。", "allowed_evidence_ids": artifacts["industry_research"]["findings"][0]["evidence_ids"], "allowed_assumption_ids": []},
                {"section": "financial", "main_point": artifacts["financial_research"]["summary"], "material_usage": "采用受信财务指标与已引用资料。", "allowed_evidence_ids": artifacts["financial_research"]["evidence_ids"], "allowed_assumption_ids": artifacts["financial_research"]["assumption_ids"]},
                {"section": "valuation", "main_point": artifacts["valuation_research"]["summary"], "material_usage": "采用受信估值结果与假设。", "allowed_evidence_ids": artifacts["valuation_research"]["evidence_ids"], "allowed_assumption_ids": artifacts["valuation_research"]["assumption_ids"]},
            ]
            return json.dumps({"symbol": symbol, "as_of": as_of, "report_mainline": artifacts["lead_final_review"]["research_thesis"], "executive_focus": "业务质量、周期、财务质量与估值假设的联动。", "sections": sections, "key_findings": artifacts["lead_final_review"]["key_findings"], "conflicts": artifacts["lead_final_review"]["conflicts"], "risks": ["公开资料与预测假设存在局限"], "missing_information": artifacts["lead_final_review"]["missing_information"]}, ensure_ascii=False)
        if profile["profile_id"] == "writer_planning":
            synthesis = context["artifacts"]["lead_synthesis"]
            legacy_sections = [{"section": item["section"], "purpose": item["main_point"], "narrative_order": index, "allowed_evidence_ids": item["allowed_evidence_ids"], "allowed_assumption_ids": item["allowed_assumption_ids"], "visual_emphasis": "trend" if item["section"] == "financial" else ("valuation" if item["section"] == "valuation" else "none")} for index, item in enumerate(synthesis["sections"], 1)]
            composition = [{"section_id": f"{item['section']}-analysis", "title": {"business": "业务基础与经营执行", "industry": "行业周期与竞争结构", "financial": "财务质量与增长验证", "valuation": "估值假设与敏感性"}[item["section"]], "purpose": item["main_point"], "narrative_order": index, "allowed_evidence_ids": item["allowed_evidence_ids"], "allowed_assumption_ids": item["allowed_assumption_ids"], "writer_group": ("financial" if item["section"] == "valuation" else item["section"]), "visual_components": (["chart", "table"] if item["section"] == "financial" else (["chart", "callout"] if item["section"] == "valuation" else ["callout"]))} for index, item in enumerate(synthesis["sections"], 1)]
            by_section = {item["section"]: item for item in synthesis["sections"]}
            visual_plan = [
                {"visual_id": "visual-performance", "section_id": "financial-analysis", "plugin_id": "financial_performance_trend", "analytical_question": "收入增长是否转化为利润增长", "source_mode": "structured", "metric_keys": ["revenue", "net_profit_attributable"], "allowed_evidence_ids": [], "allowed_assumption_ids": [], "preferred_chart_type": "combo", "unit_hint": "财务数据单位", "placement": "after_claim", "caption_focus": "比较经营规模与归母利润的变化方向", "comparison_mode": "time_series", "comparison_basis": "比较同一财务口径下收入与归母净利润的跨期变化", "priority": 1},
                {"visual_id": "visual-profitability", "section_id": "financial-analysis", "plugin_id": "profitability_quality", "analytical_question": "增长质量和股东回报如何变化", "source_mode": "structured", "metric_keys": ["gross_margin", "net_margin", "roe"], "allowed_evidence_ids": [], "allowed_assumption_ids": [], "preferred_chart_type": "line", "unit_hint": "比率", "placement": "after_body", "caption_focus": "观察利润率与 ROE 的同步性", "comparison_mode": "time_series", "comparison_basis": "比较利润率与 ROE 在相同历史期间内的变化", "priority": 2},
                {"visual_id": "visual-cashflow", "section_id": "financial-analysis", "plugin_id": "cashflow_capex", "analytical_question": "利润是否形成可持续的现金回报", "source_mode": "structured", "metric_keys": ["operating_cash_flow", "capital_expenditure", "free_cash_flow"], "allowed_evidence_ids": [], "allowed_assumption_ids": [], "preferred_chart_type": "combo", "unit_hint": "财务数据单位", "placement": "after_body", "caption_focus": "将经营现金流、资本开支与自由现金流联系观察", "comparison_mode": "time_series", "comparison_basis": "比较经营现金流、资本开支和自由现金流的跨期变化", "priority": 2},
            ]
            business_evidence = by_section.get("business", {}).get("allowed_evidence_ids", [])
            if business_evidence:
                visual_plan.append({"visual_id": "visual-business-mix", "section_id": "business-analysis", "plugin_id": "business_mix", "analytical_question": "业务结构如何变化", "source_mode": "evidence", "metric_keys": ["业务占比"], "allowed_evidence_ids": business_evidence, "allowed_assumption_ids": [], "preferred_chart_type": "stacked_bar", "unit_hint": "%", "placement": "after_body", "caption_focus": "仅呈现可在 Evidence 原文中逐点核验的数据", "comparison_mode": "composition", "comparison_basis": "比较统一占比口径下不同业务或期间的结构变化", "priority": 4})
            return json.dumps({"symbol": symbol, "as_of": as_of, "title": "个股基本面分析报告", "executive_focus": synthesis["executive_focus"], "sections": legacy_sections, "report_composition": composition, "visual_plan": visual_plan, "key_findings": synthesis["key_findings"], "risks": synthesis["risks"], "missing_information": synthesis["missing_information"]}, ensure_ascii=False)
        if profile["profile_id"] == "chart_data_extractor":
            return json.dumps({"symbol": symbol, "as_of": as_of, "candidates": []}, ensure_ascii=False)
        if profile["profile_id"] == "writer_section":
            group = context["section_group"]
            assignments = context["artifacts"]["writer_assignment"]
            written_sections = [{
                "section_id": item["section_id"],
                "title": item["title"],
                "main_claim": item["purpose"],
                "body": f"{item['purpose']} 本专题依据分配给当前章节 Writer 的已验证材料展开，围绕核心判断、事实依据、经营影响和持续观察形成连续论证。所有数字与引用均受当前材料包约束，不跨章节扩写。",
                "evidence_ids": item["allowed_evidence_ids"],
                "assumption_ids": item["allowed_assumption_ids"],
                "observation_points": ["后续披露中的关键经营指标", "专题相关外部条件变化"],
            } for item in assignments]
            return json.dumps({
                "symbol": symbol, "as_of": as_of, "section_group": group,
                "sections": written_sections,
            }, ensure_ascii=False)
        if profile["profile_id"] == "final_synthesis":
            artifacts = context["artifacts"]
            sections_by_id = {
                section["section_id"]: section
                for output in artifacts["writer_sections"].values()
                for section in output["sections"]
            }
            planned = artifacts["writer_plan"].get("report_composition", [])
            section_order = [
                item["section_id"] for item in sorted(
                    planned, key=lambda item: item["narrative_order"]
                )
                if item["section_id"] in sections_by_id
            ]
            for section_id in sections_by_id:
                if section_id not in section_order:
                    section_order.append(section_id)
            return json.dumps({
                "symbol": symbol,
                "as_of": as_of,
                "section_order": section_order,
                "text_edits": [],
                "transitions": [],
                "executive_summary": artifacts["lead_synthesis"]["executive_focus"],
                "conclusion": "全文应沿着公司业务、行业环境与财务兑现的主线连续理解。",
                "edit_summary": ["按 Writer Plan 组装三个 Writer 的专题稿"],
            }, ensure_ascii=False)
        if profile["profile_id"] == "fundamental_writer":
            artifacts = context["artifacts"]
            needs_more = self.scenario == "writer_needs_more_research"
            missing = ["缺少主要业务分部收入数据"] if needs_more else []
            composition = artifacts["writer_plan"].get("report_composition", [])
            written_sections = [{"section_id": item["section_id"], "title": item["title"], "main_claim": item["purpose"], "body": f"{item['purpose']} 本专题依据已获准的研究材料展开，重点不是重复前序摘要，而是把已验证事实放入经营、行业与财务质量的共同框架中解释。读者应结合相关数据口径、资料时点和后续披露理解这一判断。", "evidence_ids": item["allowed_evidence_ids"], "assumption_ids": item["allowed_assumption_ids"], "observation_points": ["后续披露中的量价与经营指标", "专题相关的外部条件变化"]} for item in composition]
            return json.dumps({
                "symbol": symbol,
                "as_of": as_of,
                "status": "needs_more_research" if needs_more else "completed",
                "executive_summary": "品牌基础、行业周期、财务表现和估值假设需要联合理解。",
                "business": {"summary": artifacts["business_research"]["summary"], "evidence_ids": artifacts["business_research"]["findings"][0]["evidence_ids"]},
                "industry": {"summary": artifacts["industry_research"]["summary"], "evidence_ids": artifacts["industry_research"]["findings"][0]["evidence_ids"]},
                "financial": {"summary": artifacts["financial_research"]["summary"], "evidence_ids": artifacts["financial_research"]["evidence_ids"], "assumption_ids": artifacts["financial_research"]["assumption_ids"]},
                "valuation": {"summary": artifacts["valuation_research"]["summary"], "evidence_ids": artifacts["valuation_research"]["evidence_ids"], "assumption_ids": artifacts["valuation_research"]["assumption_ids"]},
                "key_findings": artifacts["lead_final_review"]["key_findings"],
                "conflicts": artifacts["lead_final_review"]["conflicts"],
                "risks": artifacts["business_research"]["risks"] + artifacts["industry_research"]["risks"] + artifacts["financial_research"]["risks"] + artifacts["valuation_research"]["risks"],
                "missing_information": missing,
                "conclusion": "研究结论应结合证据、假设、风险和数据限制审慎理解。",
                "disclaimer": "本输出不构成投资建议、交易指令或收益承诺。",
                "sections": written_sections,
            }, ensure_ascii=False)
        if profile["mode"] == "full":
            findings: list[dict[str, Any]] = []
            if self.scenario == "tool_call":
                result = tool_handler("runtime_echo", {"message": "bridge-ok"})
                findings = [
                    {
                        "claim": f"Tool path returned {result['echo']}",
                        "evidence_ids": [],
                        "assumption_ids": [],
                        "confidence": "high",
                    }
                ]
            return _valid_output(
                "runtime_full_test", "Full Agent Runtime 验证成功", findings
            )
        assert "upstream" in context
        return _valid_output(
            "runtime_constrained_test", "Constrained Agent 已读取受控上下文", []
        )

    def repair_output(
        self,
        *,
        session_id: str,
        raw_output: str,
        validation_error: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> str:
        del session_id, raw_output, validation_error, output_schema, timeout_seconds
        if self.scenario == "schema_repair":
            return _valid_output("runtime_repaired", "Schema 修复成功", [])
        return "repair failed"

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        if session_id not in self.closed_sessions:
            self.closed_sessions.append(session_id)

    def shutdown(self) -> None:
        for session_id in list(self.sessions):
            self.close_session(session_id)


class BridgePiClient:
    """Synchronous Python client for the long-running JSON Lines bridge."""

    def __init__(
        self,
        *,
        command: str,
        entrypoint: Path,
        runtime_mode: str,
        start_timeout: float,
        request_timeout: float,
        max_restarts: int,
        model_provider: str | None,
        model_name: str | None,
        api_key_env_name: str | None,
    ) -> None:
        self.command = command
        self.entrypoint = Path(entrypoint)
        self.runtime_mode = runtime_mode
        self.start_timeout = start_timeout
        self.request_timeout = request_timeout
        self.max_restarts = max_restarts
        self.model_provider = model_provider
        self.model_name = model_name
        self.api_key_env_name = api_key_env_name
        self.protocol_errors: list[str] = []
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._restart_count = 0
        self._logger = logging.getLogger(__name__)
        self._usage_by_session: dict[str, dict[str, Any]] = {}

    def health_check(self) -> dict[str, Any]:
        return self._request("health_check", {})

    def validate_model(self) -> dict[str, Any]:
        if not self.model_provider or not self.model_name:
            raise BridgeStartError("Live mode requires PI_MODEL_PROVIDER and PI_MODEL")
        return self._request(
            "validate_model",
            {"provider": self.model_provider, "name": self.model_name},
        )

    def create_session(
        self,
        *,
        session_id: str,
        profile: dict[str, Any],
        model: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> None:
        self._request(
            "create_session",
            {
                "session_id": session_id,
                "profile": profile,
                "model": model,
                "tools": tools,
            },
        )

    def run_agent(
        self,
        *,
        session_id: str,
        system_prompt: str,
        context: dict[str, Any],
        task: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
        tool_handler: ToolCallHandler,
    ) -> str:
        payload = self._request(
            "run_agent",
            {
                "session_id": session_id,
                "system_prompt": system_prompt,
                "context": context,
                "task": task,
                "output_schema": output_schema,
            },
            timeout_seconds=timeout_seconds,
            tool_handler=tool_handler,
        )
        output = payload.get("output")
        if not isinstance(output, str):
            raise BridgeProtocolError("Bridge response missing string output")
        self._capture_usage(session_id, payload.get("usage"))
        return output

    def repair_output(
        self,
        *,
        session_id: str,
        raw_output: str,
        validation_error: str,
        output_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> str:
        payload = self._request(
            "repair_output",
            {
                "session_id": session_id,
                "raw_output": raw_output,
                "validation_error": validation_error,
                "output_schema": output_schema,
            },
            timeout_seconds=timeout_seconds,
        )
        output = payload.get("output")
        if not isinstance(output, str):
            raise BridgeProtocolError("Bridge repair response missing string output")
        self._capture_usage(session_id, payload.get("usage"))
        return output

    def pop_usage(self, session_id: str) -> dict[str, Any] | None:
        return self._usage_by_session.pop(session_id, None)

    def _capture_usage(self, session_id: str, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        current = self._usage_by_session.setdefault(
            session_id,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": None,
                "cost_currency": None,
            },
        )
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                current[name] += value
        cost = usage.get("estimated_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            current["estimated_cost"] = float(current.get("estimated_cost") or 0) + float(cost)
            current["cost_currency"] = str(usage.get("cost_currency") or "USD")[:12]

    def close_session(self, session_id: str) -> None:
        self._request("close_session", {"session_id": session_id})

    def shutdown(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        try:
            self._start_once()
        except BridgeStartError:
            if self._restart_count >= self.max_restarts:
                raise
            self._restart_count += 1
            self.shutdown()
            self._messages = queue.Queue()
            self._start_once()

    def _start_once(self) -> None:
        if not self.entrypoint.is_file():
            raise BridgeStartError(f"Bridge entrypoint not found: {self.entrypoint}")
        environment = self._restricted_environment()
        if not self.command.strip():
            raise BridgeStartError("PI_BRIDGE_COMMAND is empty")
        arguments = [self.command, str(self.entrypoint)]
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            raise BridgeStartError(f"Bridge start failed: {exc}") from exc
        self._process = process
        message_queue = self._messages
        threading.Thread(
            target=self._read_stdout, args=(process, message_queue), daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(process,), daemon=True
        ).start()
        deadline = time.monotonic() + self.start_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.shutdown()
                raise BridgeStartError("Bridge ready timeout")
            try:
                incoming = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                self.shutdown()
                raise BridgeStartError("Bridge ready timeout") from exc
            if incoming.get("type") == "ready":
                return
            if incoming.get("type") == "_bridge_exit":
                self._process = None
                raise BridgeStartError("Bridge exited before ready")
            self.protocol_errors.append("Unexpected message before ready")

    def _request(
        self,
        request_type: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_handler: ToolCallHandler | None = None,
    ) -> dict[str, Any]:
        with self._request_lock:
            self._start()
            request_id = str(uuid4())
            self._send({"id": request_id, "type": request_type, "payload": payload})
            deadline = time.monotonic() + (timeout_seconds or self.request_timeout)
            deferred_tool_error: Exception | None = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_timed_out_bridge()
                    raise AgentTimeoutError(f"Bridge request timeout: {request_type}")
                try:
                    incoming = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    self._terminate_timed_out_bridge()
                    raise AgentTimeoutError(
                        f"Bridge request timeout: {request_type}"
                    ) from exc
                incoming_type = incoming.get("type")
                if incoming_type == "_bridge_exit":
                    self._restart_after_crash()
                    raise BridgeCrashedError(f"Bridge exited during {request_type}")
                if incoming_type == "tool_call":
                    if incoming.get("payload", {}).get("request_id") != request_id:
                        raise BridgeProtocolError("Tool call crossed request boundary")
                    if tool_handler is None:
                        deferred_tool_error = BridgeProtocolError("Unexpected tool call")
                        self._send(
                            {
                                "id": incoming.get("id"),
                                "type": "tool_result",
                                "payload": {"error": "Unexpected tool call"},
                            }
                        )
                        continue
                    call_payload = incoming.get("payload", {})
                    try:
                        result = tool_handler(
                            str(call_payload.get("tool_name", "")),
                            dict(call_payload.get("arguments", {})),
                        )
                        # A later successful tool call means the model recovered
                        # from an ordinary tool error after seeing its result.
                        # Permission and protocol failures remain authoritative.
                        if deferred_tool_error is not None and not isinstance(
                            deferred_tool_error,
                            (ToolNotAllowedError, BridgeProtocolError),
                        ):
                            deferred_tool_error = None
                        tool_payload: dict[str, Any] = {"result": result}
                    except ToolBudgetExhaustedError as exc:
                        # Budget exhaustion is a normal terminal signal for a
                        # Full Agent: return it as a tool result so the model can
                        # produce a partial, evidence-backed final JSON instead
                        # of aborting the entire Bridge request.
                        tool_payload = {
                            "result": {
                                "status": "tool_budget_exhausted",
                                "message": str(exc),
                            }
                        }
                    except Exception as exc:
                        deferred_tool_error = exc
                        tool_payload = {"error": str(exc)}
                        self._send(
                            {
                                "id": incoming.get("id"),
                                "type": "tool_result",
                                "payload": tool_payload,
                            }
                        )
                        continue
                    self._send(
                        {
                            "id": incoming.get("id"),
                            "type": "tool_result",
                            "payload": tool_payload,
                        }
                    )
                    continue
                if incoming_type == "agent_event":
                    if incoming.get("id") != request_id:
                        raise BridgeProtocolError(
                            "Agent event crossed request boundary"
                        )
                    continue
                if incoming.get("id") != request_id:
                    raise BridgeProtocolError("Bridge response request id mismatch")
                if incoming_type == "error":
                    if deferred_tool_error is not None:
                        raise deferred_tool_error
                    error_payload = incoming.get("payload", {})
                    raise BridgeProtocolError(str(error_payload.get("message", "Bridge error")))
                if incoming_type not in {"response", "session_closed"}:
                    raise BridgeProtocolError(f"Unexpected bridge message: {incoming_type}")
                response_payload = incoming.get("payload")
                if not isinstance(response_payload, dict):
                    raise BridgeProtocolError("Bridge payload must be an object")
                if isinstance(
                    deferred_tool_error,
                    (ToolNotAllowedError, BridgeProtocolError),
                ):
                    raise deferred_tool_error
                return response_payload

    def _send(self, value: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise BridgeCrashedError("Bridge is not running")
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(serialized + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise BridgeCrashedError("Bridge stdin closed") from exc

    def _read_stdout(
        self,
        process: subprocess.Popen[str],
        output_queue: queue.Queue[dict[str, Any]],
    ) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                incoming = json.loads(line)
                if not isinstance(incoming, dict):
                    raise ValueError("protocol line is not an object")
                output_queue.put(incoming)
            except (json.JSONDecodeError, ValueError) as exc:
                self.protocol_errors.append(str(exc))
                output_queue.put(
                    {
                        "type": "error",
                        "id": "invalid",
                        "payload": {"message": "Bridge stdout contained non-protocol data"},
                    }
                )
        output_queue.put({"type": "_bridge_exit", "payload": {}})

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            safe_line = safe_error_message(line.rstrip())
            if self.runtime_mode == "live" and self.api_key_env_name:
                value = os.environ.get(self.api_key_env_name)
                if value:
                    safe_line = safe_line.replace(value, "[REDACTED]")
            self._logger.debug("Pi Bridge: %s", safe_line)

    def _restart_after_crash(self) -> None:
        self._process = None
        if self._restart_count >= self.max_restarts:
            return
        self._restart_count += 1
        self._messages = queue.Queue()
        self._start_once()

    def _terminate_timed_out_bridge(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self._messages = queue.Queue()

    def _restricted_environment(self) -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PI_RUNTIME_MODE": self.runtime_mode,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        if self.runtime_mode == "live":
            if not self.model_provider or not self.model_name:
                raise BridgeStartError("Live mode requires PI_MODEL_PROVIDER and PI_MODEL")
            name = self.api_key_env_name or ""
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise BridgeStartError("PI_API_KEY_ENV_NAME is invalid")
            value = os.environ.get(name)
            if not value:
                raise BridgeStartError(f"Live API key environment variable is missing: {name}")
            provider_key_names = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }
            provider_key_name = provider_key_names.get(self.model_provider)
            if provider_key_name is None:
                raise BridgeStartError("Unsupported live model provider")
            environment[name] = value
            # The parent accepts a configurable secret name, while the Pi
            # provider SDK reads its conventional variable in the restricted
            # child environment. The value is never serialized into JSONL.
            environment[provider_key_name] = value
        return environment
