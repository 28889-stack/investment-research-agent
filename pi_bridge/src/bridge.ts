import { randomUUID } from "node:crypto";

import { message, type ProtocolMessage, type SessionRecord } from "./protocol.js";
import { LiveAgentSession, validateLiveModel } from "./live-runtime.js";

type Sender = (value: ProtocolMessage) => void;

interface PendingTool {
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: Error) => void;
}

function validOutput(
  taskId: string,
  summary: string,
  findings: Record<string, unknown>[] = [],
): string {
  return JSON.stringify({
    task_id: taskId,
    status: "completed",
    summary,
    findings,
    new_evidence: [],
    new_assumptions: [],
    risks: [],
    conflicts: [],
    missing_information: [],
    suggested_followups: [],
  });
}

export class Bridge {
  private readonly sessions = new Map<string, SessionRecord>();
  private readonly pendingTools = new Map<string, PendingTool>();
  private readonly liveSessions = new Map<string, LiveAgentSession>();

  constructor(
    private readonly send: Sender,
    private readonly runtimeMode = process.env.PI_RUNTIME_MODE ?? "mock",
  ) {}

  handle(request: ProtocolMessage): void {
    if (request.type === "tool_result") {
      this.acceptToolResult(request);
      return;
    }
    void this.dispatch(request).catch((error: unknown) => {
      this.send(
        message(request.id, "error", {
          code: "BRIDGE_REQUEST_ERROR",
          message: error instanceof Error ? error.message : String(error),
        }),
      );
    });
  }

  private async dispatch(request: ProtocolMessage): Promise<void> {
    switch (request.type) {
      case "health_check":
        this.send(
          message(request.id, "response", {
            status: "ok",
            runtime_mode: this.runtimeMode,
            sessions: this.sessions.size,
          }),
        );
        return;
      case "validate_model": {
        if (this.runtimeMode !== "live") {
          throw new Error("validate_model requires live runtime mode");
        }
        const provider = String(request.payload.provider ?? "");
        const name = String(request.payload.name ?? "");
        validateLiveModel(provider, name);
        this.send(message(request.id, "response", { status: "ok", provider, name }));
        return;
      }
      case "create_session":
        this.createSession(request);
        return;
      case "run_agent":
        await this.runAgent(request);
        return;
      case "repair_output":
        await this.repairOutput(request);
        return;
      case "close_session":
        this.closeSession(request);
        return;
      default:
        throw new Error(`Unsupported request type: ${request.type}`);
    }
  }

  private createSession(request: ProtocolMessage): void {
    const sessionId = String(request.payload.session_id ?? "");
    if (!sessionId) throw new Error("session_id is required");
    if (this.sessions.has(sessionId)) throw new Error("session already exists");
    const profile = request.payload.profile;
    const model = request.payload.model;
    const tools = request.payload.tools;
    if (!profile || typeof profile !== "object") throw new Error("profile is required");
    if (!model || typeof model !== "object") throw new Error("model is required");
    if (!Array.isArray(tools)) throw new Error("tools must be an array");
    const record = {
      sessionId,
      profile,
      model,
      tools,
    } as SessionRecord;
    if (record.profile.mode === "constrained" && record.tools.length > 0) {
      throw new Error("constrained session cannot expose tools");
    }
    const permitted = new Set(
      Array.isArray(record.profile.allowed_tools)
        ? (record.profile.allowed_tools as string[])
        : [],
    );
    if (record.tools.some((tool) => !permitted.has(tool.name))) {
      throw new Error("bridge tool list exceeds profile permissions");
    }
    this.sessions.set(sessionId, record);
    this.send(message(request.id, "response", { session_id: sessionId }));
  }

  private async runAgent(request: ProtocolMessage): Promise<void> {
    const sessionId = String(request.payload.session_id ?? "");
    const session = this.sessions.get(sessionId);
    if (!session) throw new Error("session not found");
    const invoke = (name: string, arguments_: Record<string, unknown>) =>
      this.invokePythonTool(request.id, sessionId, name, arguments_);
    const context = (request.payload.context ?? {}) as Record<string, unknown>;
    const outputSchema = (request.payload.output_schema ?? {}) as Record<string, unknown>;
    this.send(
      message(request.id, "agent_event", {
        session_id: sessionId,
        event: "started",
      }),
    );
    let output: string;
    let usage: unknown;
    if (this.runtimeMode === "mock") {
      output = await this.runMock(session, invoke, context);
    } else if (this.runtimeMode === "live") {
      let liveSession = this.liveSessions.get(sessionId);
      if (!liveSession) {
        liveSession = new LiveAgentSession(
          session,
          String(request.payload.system_prompt ?? ""),
        );
        this.liveSessions.set(sessionId, liveSession);
      }
      const result = await liveSession.run(
        context,
        String(request.payload.task ?? ""),
        outputSchema,
        invoke,
      );
      output = result.output;
      usage = result.usage;
    } else {
      throw new Error(`Unsupported PI_RUNTIME_MODE: ${this.runtimeMode}`);
    }
    this.send(
      message(request.id, "agent_event", {
        session_id: sessionId,
        event: "completed",
      }),
    );
    this.send(message(request.id, "response", usage ? { output, usage } : { output }));
  }

  private async runMock(
    session: SessionRecord,
    invoke: (name: string, args: Record<string, unknown>) => Promise<Record<string, unknown>>,
    context: Record<string, unknown>,
  ): Promise<string> {
    if (session.profile.profile_id === "technical_research") {
      const market = await invoke("get_market_data", {});
      await invoke("calculate_technical_indicators", {});
      const summary = await invoke("get_technical_summary", {});
      const trend = summary.trend as Record<string, unknown>;
      const macd = summary.macd as Record<string, unknown>;
      const conflicts =
        trend.alignment === "bullish" && macd.cross === "bearish"
          ? ["均线趋势偏强，但 MACD 当前状态偏弱"]
          : [];
      return JSON.stringify({
        symbol: market.symbol,
        as_of: market.as_of,
        data_version: market.data_version,
        trend: `均线排列状态为 ${String(trend.alignment)}，趋势信号由指标脚本确认。`,
        volume_price: "成交量与价格关系以脚本输出的均量信号为依据。",
        momentum: "MACD、RSI 与 KDJ 信号存在不同观察周期。",
        volatility: "历史波动率和 ATR 反映近期波动区间。",
        support_resistance: "支撑与阻力采用近期和中期价格区间极值。",
        patterns: summary.patterns,
        short_term: "短期关注动量信号及近期价格区间。",
        medium_term: "中期关注中期与长期均线的相对关系。",
        long_term: "长期结论受当前历史窗口限制，需要持续观察。",
        conflicts,
        risks: ["技术指标存在滞后性", "历史行情不能保证未来表现"],
        confidence: "medium",
      });
    }
    if (session.profile.profile_id === "technical_assembly") {
      const research = context.technical_research as Record<string, unknown>;
      const kronos = context.kronos as Record<string, unknown>;
      const probabilities = kronos.direction_probability as Record<string, number>;
      const dominant = Object.entries(probabilities).sort((left, right) => right[1] - left[1])[0]?.[0];
      const conflicts = Array.isArray(research.conflicts)
        ? [...research.conflicts] as unknown[]
        : [];
      if (dominant === "flat" && String(research.medium_term).includes("偏强")) {
        conflicts.push("技术指标中期偏强，而 Kronos 方向概率以震荡为主");
      }
      return JSON.stringify({
        symbol: research.symbol,
        as_of: research.as_of,
        data_version: research.data_version,
        summary: "技术指标解释与 Kronos 预测已按同一数据版本完成对比。",
        agreements: ["两类结果均基于同一标准化历史行情"],
        conflicts,
        uncertainties: ["模型概率与技术指标均不能消除未来不确定性"],
        short_term: research.short_term,
        medium_term: research.medium_term,
        long_term: research.long_term,
        risks: research.risks,
        conclusion: "当前信号需结合冲突与不确定性审慎理解。",
        disclaimer: "本输出不构成投资建议或交易指令。",
      });
    }
    const node = String(context.node ?? "");
    const run = (context.run ?? {}) as Record<string, unknown>;
    const symbol = String(run.resolved_symbol ?? run.symbol ?? "");
    const asOf = String(run.as_of ?? "");
    if (session.profile.profile_id === "fundamental_lead" && node === "lead_planning") {
      await invoke("get_company_profile", {});
      const sources = await invoke("search_research_sources", { query: "公司业务与年报" });
      const sourceItems = sources.items as Record<string, unknown>[];
      const evidence = await invoke("read_research_source", {
        result_id: sourceItems[0]?.result_id,
        claim: "公司主要业务与经营特征",
        evidence_type: "historical_fact",
      });
      return JSON.stringify({
        symbol,
        as_of: asOf,
        thesis: "研究主线聚焦品牌壁垒、行业需求、现金流与估值假设。",
        key_questions: ["需求和渠道的可持续性如何"],
        business_scope: ["产品结构", "渠道与品牌壁垒"],
        industry_scope: ["供需和竞争格局"],
        financial_focus: ["增长", "盈利能力", "自由现金流"],
        valuation_focus: ["相对估值", "简化 DCF"],
        risks_to_verify: ["行业需求波动", "估值假设敏感性"],
        evidence_ids: [evidence.evidence_id],
      });
    }
    if (session.profile.profile_id === "business_research") {
      await invoke("get_company_profile", {});
      const sources = await invoke("search_research_sources", { query: "商业模式产品渠道" });
      const sourceItems = sources.items as Record<string, unknown>[];
      const evidence = await invoke("read_research_source", { result_id: sourceItems[0]?.result_id, claim: "公司商业模式和渠道", evidence_type: "historical_fact" });
      return JSON.stringify({ symbol, summary: "公司业务聚焦核心品牌与酒类产品。", findings: [{ claim: "品牌与渠道是商业模式的关键环节。", evidence_ids: [evidence.evidence_id], confidence: "medium" }], risks: ["需求与渠道变化风险"], missing_information: [] });
    }
    if (session.profile.profile_id === "industry_research") {
      const sources = await invoke("search_research_sources", { query: "白酒行业供需与竞争" });
      const sourceItems = sources.items as Record<string, unknown>[];
      const selected = sourceItems[1] ?? sourceItems[0];
      const evidence = await invoke("read_research_source", { result_id: selected?.result_id, claim: "行业供需与竞争格局", evidence_type: "third_party_forecast" });
      return JSON.stringify({ symbol, summary: "行业需求具有周期性，头部品牌优势与竞争并存。", findings: [{ claim: "行业竞争聚焦品牌和渠道。", evidence_ids: [evidence.evidence_id], confidence: "medium" }], risks: ["周期与政策变化风险"], missing_information: [] });
    }
    const artifacts = (context.artifacts ?? {}) as Record<string, Record<string, unknown>>;
    if (session.profile.profile_id === "fundamental_lead" && node === "lead_review") {
      const business = artifacts.business_research ?? {};
      const industry = artifacts.industry_research ?? {};
      const missing = [...((business.missing_information ?? []) as unknown[]), ...((industry.missing_information ?? []) as unknown[])];
      return JSON.stringify({ symbol, business_status: "accepted", industry_status: missing.length ? "accepted_with_gaps" : "accepted", key_findings: ["品牌渠道优势与行业周期风险并存"], conflicts: ["业务稳定性与行业周期波动需继续验证"], financial_questions: ["自由现金流能否支持长期估值"], missing_information: missing, followup_research_tasks: ["补充关键经营与行业数据的具体口径、期间和来源"] });
    }
    if (session.profile.profile_id === "deep_research") {
      const review = artifacts.lead_review ?? {};
      const tasks = [
        ...((review.followup_research_tasks ?? []) as unknown[]),
        ...((review.missing_information ?? []) as unknown[]),
        ...((review.financial_questions ?? []) as unknown[]),
      ].map(String);
      const sources = await invoke("search_research_sources", { query: tasks.join("；") || "补充 Lead Review 缺失数据" });
      const sourceItems = sources.items as Record<string, unknown>[];
      const evidence = await invoke("read_research_source", {
        result_id: sourceItems[0]?.result_id,
        claim: tasks[0] ?? "补充 Lead Review 缺失数据",
        evidence_type: "historical_fact",
      });
      return JSON.stringify({
        symbol,
        summary: "已按 Lead Review 的补充任务完成一轮深度检索。",
        findings: [{ claim: tasks[0] ?? "补充数据已检索", evidence_ids: [evidence.evidence_id], confidence: "medium" }],
        risks: [],
        missing_information: (review.missing_information ?? []) as unknown[],
      });
    }
    if (session.profile.profile_id === "financial_research") {
      return JSON.stringify({ symbol, summary: "历史财务数据显示盈利与现金流需结合增长假设解读。", growth_analysis: "关注收入与归母净利涨幅的匹配。", profitability_analysis: "关注利润率和净资产收益率的持续性。", cash_flow_analysis: "经营现金流与自由现金流是估值的关键输入。", balance_sheet_analysis: "现金与负债结构影响股权价值。", earnings_drivers: ["产品结构", "渠道效率", "需求变化"], assumptions: [{ variable: "fcf_growth", value: 0.08, period: "FY2026-FY2030", source: "financial_research" }, { variable: "terminal_growth", value: 0.03, period: "terminal", source: "financial_research" }, { variable: "discount_rate", value: 0.10, period: "forecast", source: "financial_research" }], risks: ["假设对估值结果敏感"], evidence_ids: ["ev_001"], confidence: "medium" });
    }
    if (session.profile.profile_id === "valuation_research") {
      const assumptions = ((artifacts.assumptions ?? {}).items ?? []) as Record<string, unknown>[];
      const valuation = artifacts.valuation_result ?? {};
      const relative = (valuation.relative ?? {}) as Record<string, Record<string, unknown>>;
      const methods = Object.entries(relative).filter(([, value]) => value.status === "available").map(([name]) => name.toUpperCase());
      const dcf = (valuation.dcf ?? {}) as Record<string, unknown>;
      if (dcf.status === "available") methods.push("DCF");
      return JSON.stringify({ symbol, summary: "相对估值与 DCF 已由 Python 脚本计算。", methods_used: methods, interpretation: "估值结果应与业务质量和数据局限一并理解。", sensitivity: "增长假设变化会改变 DCF 结果。", risks: ["市场价格和关键假设可变"], assumption_ids: assumptions.map((item) => item.id), evidence_ids: ["ev_001"], confidence: "medium" });
    }
    if (session.profile.profile_id === "fundamental_lead" && node === "lead_final_review") {
      const lead = artifacts.lead_plan ?? {};
      const review = artifacts.lead_review ?? {};
      const deep = artifacts.deep_research ?? {};
      const missing = [...((review.missing_information ?? []) as unknown[]), ...((deep.missing_information ?? []) as unknown[])];
      return JSON.stringify({ symbol, research_thesis: lead.thesis, approved_sections: ["business", "industry", "financial", "valuation"], key_findings: review.key_findings ?? [], conflicts: review.conflicts ?? [], missing_information: missing, report_outline: ["公司业务", "行业", "财务", "估值", "风险"], ready_for_writer: missing.length === 0 });
    }
    if (session.profile.profile_id === "lead_synthesis") {
      const business = artifacts.business_research ?? {};
      const industry = artifacts.industry_research ?? {};
      const financial = artifacts.financial_research ?? {};
      const valuation = artifacts.valuation_research ?? {};
      const finalReview = artifacts.lead_final_review ?? {};
      const businessFindings = (business.findings ?? []) as Record<string, unknown>[];
      const industryFindings = (industry.findings ?? []) as Record<string, unknown>[];
      const sections = [
        { section: "business", main_point: business.summary, material_usage: "采用业务研究简报中已引用的来源。", allowed_evidence_ids: businessFindings[0]?.evidence_ids ?? [], allowed_assumption_ids: [] },
        { section: "industry", main_point: industry.summary, material_usage: "采用行业研究简报中已引用的来源。", allowed_evidence_ids: industryFindings[0]?.evidence_ids ?? [], allowed_assumption_ids: [] },
        { section: "financial", main_point: financial.summary, material_usage: "采用受信财务指标与已引用资料。", allowed_evidence_ids: financial.evidence_ids ?? [], allowed_assumption_ids: financial.assumption_ids ?? [] },
        { section: "valuation", main_point: valuation.summary, material_usage: "采用受信估值结果与假设。", allowed_evidence_ids: valuation.evidence_ids ?? [], allowed_assumption_ids: valuation.assumption_ids ?? [] },
      ];
      return JSON.stringify({ symbol, as_of: asOf, report_mainline: finalReview.research_thesis, executive_focus: "业务质量、周期、财务质量与估值假设的联动。", sections, key_findings: finalReview.key_findings ?? [], conflicts: finalReview.conflicts ?? [], risks: ["公开资料与预测假设存在局限"], missing_information: finalReview.missing_information ?? [] });
    }
    if (session.profile.profile_id === "writer_planning") {
      const synthesis = artifacts.lead_synthesis ?? {};
      const sections = (synthesis.sections ?? []) as Record<string, unknown>[];
      return JSON.stringify({
        symbol,
        as_of: asOf,
        title: "个股基本面分析报告",
        executive_focus: synthesis.executive_focus,
        sections: sections.map((item, index) => ({
          section: item.section,
          purpose: item.main_point,
          narrative_order: index + 1,
          allowed_evidence_ids: item.allowed_evidence_ids ?? [],
          allowed_assumption_ids: item.allowed_assumption_ids ?? [],
          visual_emphasis: item.section === "financial" ? "trend" : (item.section === "valuation" ? "valuation" : "none"),
        })),
        key_findings: synthesis.key_findings ?? [],
        risks: synthesis.risks ?? [],
        missing_information: synthesis.missing_information ?? [],
      });
    }
    if (session.profile.profile_id === "fundamental_writer") {
      const business = artifacts.business_research ?? {};
      const industry = artifacts.industry_research ?? {};
      const financial = artifacts.financial_research ?? {};
      const valuation = artifacts.valuation_research ?? {};
      const finalReview = artifacts.lead_final_review ?? {};
      const businessFindings = (business.findings ?? []) as Record<string, unknown>[];
      const industryFindings = (industry.findings ?? []) as Record<string, unknown>[];
      return JSON.stringify({
        symbol,
        as_of: asOf,
        status: "completed",
        executive_summary: "品牌基础、行业周期、财务表现和估值假设需要联合理解。",
        business: { summary: business.summary, evidence_ids: businessFindings[0]?.evidence_ids ?? [] },
        industry: { summary: industry.summary, evidence_ids: industryFindings[0]?.evidence_ids ?? [] },
        financial: { summary: financial.summary, evidence_ids: financial.evidence_ids ?? [], assumption_ids: financial.assumption_ids ?? [] },
        valuation: { summary: valuation.summary, evidence_ids: valuation.evidence_ids ?? [], assumption_ids: valuation.assumption_ids ?? [] },
        key_findings: finalReview.key_findings ?? [],
        conflicts: finalReview.conflicts ?? [],
        risks: [
          ...((business.risks ?? []) as unknown[]),
          ...((industry.risks ?? []) as unknown[]),
          ...((financial.risks ?? []) as unknown[]),
          ...((valuation.risks ?? []) as unknown[]),
        ],
        missing_information: [],
        conclusion: "研究结论应结合证据、假设、风险和数据限制审慎理解。",
        disclaimer: "本输出不构成投资建议、交易指令或收益承诺。",
      });
    }
    if (session.profile.mode === "full") {
      const echo = session.tools.find((tool) => tool.name === "runtime_echo");
      if (!echo) throw new Error("Full smoke session requires runtime_echo");
      const result = await invoke("runtime_echo", { message: "bridge-ok" });
      return validOutput("runtime_full_test", "Full Agent Runtime 验证成功", [
        {
          claim: `Python ToolRegistry 返回 ${String(result.echo)}`,
          evidence_ids: [],
          assumption_ids: [],
          confidence: "high",
        },
      ]);
    }
    return validOutput(
      "runtime_constrained_test",
      "Constrained Agent 已读取受控上下文",
    );
  }

  private async repairOutput(request: ProtocolMessage): Promise<void> {
    const sessionId = String(request.payload.session_id ?? "");
    const session = this.sessions.get(sessionId);
    if (!session) throw new Error("session not found");
    this.send(
      message(request.id, "agent_event", {
        session_id: sessionId,
        event: "repair_started",
      }),
    );
    let output: string;
    let usage: unknown;
    if (this.runtimeMode === "mock") {
      output = validOutput("runtime_repaired", "Schema 修复成功");
    } else if (this.runtimeMode === "live") {
      const liveSession = this.liveSessions.get(sessionId);
      if (!liveSession) throw new Error("live session has not run yet");
      const result = await liveSession.repair(
        String(request.payload.raw_output ?? ""),
        String(request.payload.validation_error ?? ""),
        (request.payload.output_schema ?? {}) as Record<string, unknown>,
      );
      output = result.output;
      usage = result.usage;
    } else {
      throw new Error(`Unsupported PI_RUNTIME_MODE: ${this.runtimeMode}`);
    }
    this.send(
      message(request.id, "agent_event", {
        session_id: sessionId,
        event: "repair_completed",
      }),
    );
    this.send(message(request.id, "response", usage ? { output, usage } : { output }));
  }

  private invokePythonTool(
    requestId: string,
    sessionId: string,
    name: string,
    arguments_: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const callId = randomUUID();
    return new Promise((resolve, reject) => {
      this.pendingTools.set(callId, { resolve, reject });
      this.send(
        message(callId, "tool_call", {
          request_id: requestId,
          session_id: sessionId,
          tool_name: name,
          arguments: arguments_,
        }),
      );
    });
  }

  private acceptToolResult(request: ProtocolMessage): void {
    const pending = this.pendingTools.get(request.id);
    if (!pending) return;
    this.pendingTools.delete(request.id);
    if (request.payload.error) {
      pending.reject(new Error(String(request.payload.error)));
      return;
    }
    pending.resolve((request.payload.result ?? {}) as Record<string, unknown>);
  }

  private closeSession(request: ProtocolMessage): void {
    const sessionId = String(request.payload.session_id ?? "");
    if (!this.sessions.delete(sessionId)) throw new Error("session not found");
    this.liveSessions.get(sessionId)?.close();
    this.liveSessions.delete(sessionId);
    this.send(message(request.id, "session_closed", { session_id: sessionId }));
  }
}
