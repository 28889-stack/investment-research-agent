import assert from "node:assert/strict";
import test from "node:test";

import { Bridge } from "./bridge.js";
import type { ProtocolMessage } from "./protocol.js";
import { LiveAgentSession, summarizeUsage, validateLiveModel } from "./live-runtime.js";

function request(id: string, type: string, payload: Record<string, unknown>): ProtocolMessage {
  return { id, type, payload };
}

function profile(mode: "full" | "constrained") {
  return {
    profile_id: `${mode}_runtime_smoke`,
    version: "v1",
    mode,
    max_iterations: mode === "full" ? 3 : 1,
    max_tool_calls: mode === "full" ? 3 : 0,
    allowed_tools: mode === "full" ? ["runtime_echo"] : [],
  };
}

const echoTool = {
  name: "runtime_echo",
  description: "echo",
  input_schema: { type: "object" },
  output_schema: { type: "object" },
};

test("live DeepSeek sessions enable medium thinking", () => {
  const session = new LiveAgentSession(
    {
      sessionId: "deepseek-thinking-session",
      profile: profile("constrained"),
      model: { provider: "deepseek", name: "deepseek-v4-pro", runtime_mode: "live" },
      tools: [],
    },
    "test",
  );

  const state = (
    session as unknown as { agent: { state: { thinkingLevel: string } } }
  ).agent.state;
  assert.equal(state.thinkingLevel, "medium");
  session.close();
});

test("health, isolated sessions, tool roundtrip, and close preserve request ids", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => {
    messages.push(value);
    if (value.type === "tool_call") {
      bridge.handle(request(value.id, "tool_result", { result: { echo: "bridge-ok" } }));
    }
  }, "mock");

  bridge.handle(request("health-1", "health_check", {}));
  bridge.handle(request("create-full", "create_session", {
    session_id: "full-session",
    profile: profile("full"),
    model: { runtime_mode: "mock" },
    tools: [echoTool],
  }));
  bridge.handle(request("create-limited", "create_session", {
    session_id: "limited-session",
    profile: profile("constrained"),
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-full", "run_agent", {
    session_id: "full-session",
    system_prompt: "test",
    context: {},
    task: "test",
    output_schema: {},
  }));
  bridge.handle(request("run-limited", "run_agent", {
    session_id: "limited-session",
    system_prompt: "test",
    context: { upstream: {} },
    task: "test",
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));

  const full = messages.find((value) => value.id === "run-full" && value.type === "response");
  const limited = messages.find((value) => value.id === "run-limited" && value.type === "response");
  assert.ok(full);
  assert.ok(limited);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 1);
  assert.equal(messages.filter((value) => value.type === "agent_event").length, 4);
  assert.match(String(full.payload.output), /runtime_full_test/);
  assert.match(String(limited.payload.output), /runtime_constrained_test/);

  bridge.handle(request("close-full", "close_session", { session_id: "full-session" }));
  bridge.handle(request("close-limited", "close_session", { session_id: "limited-session" }));
  assert.ok(messages.some((value) => value.id === "close-full" && value.type === "session_closed"));
  assert.ok(messages.some((value) => value.id === "close-limited" && value.type === "session_closed"));
});

test("bridge refuses tools outside profile and unknown requests", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => messages.push(value), "mock");
  bridge.handle(request("bad-session", "create_session", {
    session_id: "bad",
    profile: profile("constrained"),
    model: {},
    tools: [echoTool],
  }));
  bridge.handle(request("bad-type", "shell", {}));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(messages.some((value) => value.id === "bad-session" && value.type === "error"));
  assert.ok(messages.some((value) => value.id === "bad-type" && value.type === "error"));
});

test("technical research mock calls the three approved Python tools", async () => {
  const messages: ProtocolMessage[] = [];
  const results: Record<string, Record<string, unknown>> = {
    get_market_data: {
      symbol: "600519.SH",
      as_of: "2026-08-05",
      data_version: "v1",
    },
    calculate_technical_indicators: { ok: true },
    get_technical_summary: {
      trend: { alignment: "bullish" },
      macd: { cross: "bullish" },
      patterns: ["均线多头排列"],
    },
  };
  const bridge = new Bridge((value) => {
    messages.push(value);
    if (value.type === "tool_call") {
      const name = String(value.payload.tool_name);
      bridge.handle(request(value.id, "tool_result", { result: results[name] }));
    }
  }, "mock");
  const tools = Object.keys(results).map((name) => ({ ...echoTool, name }));
  bridge.handle(request("create-tech", "create_session", {
    session_id: "tech-session",
    profile: {
      ...profile("full"),
      profile_id: "technical_research",
      allowed_tools: Object.keys(results),
      max_tool_calls: 5,
    },
    model: { runtime_mode: "mock" },
    tools,
  }));
  bridge.handle(request("run-tech", "run_agent", {
    session_id: "tech-session",
    context: {},
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const output = messages.find((value) => value.id === "run-tech" && value.type === "response");
  assert.ok(output);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 3);
  assert.match(String(output.payload.output), /600519.SH/);
});

test("fundamental lead mock uses only profile, search, and evidence tools", async () => {
  const messages: ProtocolMessage[] = [];
  const results: Record<string, Record<string, unknown>> = {
    get_company_profile: { symbol: "600519.SH", short_name: "贵州茅台" },
    search_research_sources: { items: [{ result_id: "src_001" }] },
    read_research_source: { evidence_id: "ev_001", source_name: "年报" },
  };
  const bridge = new Bridge((value) => {
    messages.push(value);
    if (value.type === "tool_call") {
      bridge.handle(request(value.id, "tool_result", { result: results[String(value.payload.tool_name)] }));
    }
  }, "mock");
  const tools = Object.keys(results).map((name) => ({ ...echoTool, name }));
  bridge.handle(request("create-lead", "create_session", {
    session_id: "lead-session",
    profile: { ...profile("full"), profile_id: "fundamental_lead", allowed_tools: Object.keys(results), max_tool_calls: 5 },
    model: { runtime_mode: "mock" },
    tools,
  }));
  bridge.handle(request("run-lead", "run_agent", {
    session_id: "lead-session",
    context: { node: "lead_planning", run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" } },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const output = messages.find((value) => value.id === "run-lead" && value.type === "response");
  assert.ok(output);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 3);
  assert.match(String(output.payload.output), /ev_001/);
  assert.match(String(output.payload.output), /business_scope/);
});

test("deep research mock follows Lead follow-up tasks and reads one source", async () => {
  const messages: ProtocolMessage[] = [];
  const results: Record<string, Record<string, unknown>> = {
    search_research_sources: { items: [{ result_id: "src_deep_001" }] },
    read_research_source: { evidence_id: "ev_deep_001", source_name: "年报" },
  };
  const bridge = new Bridge((value) => {
    messages.push(value);
    if (value.type === "tool_call") {
      bridge.handle(request(value.id, "tool_result", { result: results[String(value.payload.tool_name)] }));
    }
  }, "mock");
  const tools = Object.keys(results).map((name) => ({ ...echoTool, name }));
  bridge.handle(request("create-deep", "create_session", {
    session_id: "deep-session",
    profile: { ...profile("full"), profile_id: "deep_research", allowed_tools: Object.keys(results), max_tool_calls: 10 },
    model: { runtime_mode: "mock" },
    tools,
  }));
  bridge.handle(request("run-deep", "run_agent", {
    session_id: "deep-session",
    context: {
      node: "deep_research",
      run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" },
      artifacts: {
        lead_review: {
          followup_research_tasks: ["补充矿产金产量与完全成本"],
          missing_information: ["缺少矿产金产量"],
          financial_questions: ["补充铜金业务收入占比"],
        },
      },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const output = messages.find((value) => value.id === "run-deep" && value.type === "response");
  assert.ok(output);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 2);
  assert.match(String(output.payload.output), /ev_deep_001/);
  assert.match(String(output.payload.output), /矿产金/);
});

test("financial and valuation mocks remain constrained and return typed outputs", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => messages.push(value), "mock");
  bridge.handle(request("create-financial", "create_session", {
    session_id: "financial-session",
    profile: { ...profile("constrained"), profile_id: "financial_research" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-financial", "run_agent", {
    session_id: "financial-session",
    context: { node: "financial_research", run: { resolved_symbol: "600519.SH" }, artifacts: {} },
    output_schema: {},
  }));
  bridge.handle(request("create-valuation", "create_session", {
    session_id: "valuation-session",
    profile: { ...profile("constrained"), profile_id: "valuation_research" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-valuation", "run_agent", {
    session_id: "valuation-session",
    context: {
      node: "valuation_research",
      run: { resolved_symbol: "600519.SH" },
      artifacts: {
        assumptions: { items: [{ id: "asm_001" }] },
        valuation_result: { relative: {}, dcf: { status: "unavailable" } },
      },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 0);
  const financial = messages.find((value) => value.id === "run-financial" && value.type === "response");
  const valuation = messages.find((value) => value.id === "run-valuation" && value.type === "response");
  assert.ok(financial);
  assert.ok(valuation);
  assert.match(String(financial.payload.output), /fcf_growth/);
  assert.match(String(valuation.payload.output), /asm_001/);
});

test("fundamental writer mock remains constrained and emits no tool calls", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => messages.push(value), "mock");
  bridge.handle(request("create-writer", "create_session", {
    session_id: "writer-session",
    profile: { ...profile("constrained"), profile_id: "fundamental_writer" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-writer", "run_agent", {
    session_id: "writer-session",
    context: {
      node: "fundamental_writer",
      run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" },
      artifacts: {
        business_research: { summary: "业务", findings: [{ evidence_ids: ["ev_001"] }], risks: [] },
        industry_research: { summary: "行业", findings: [{ evidence_ids: ["ev_002"] }], risks: [] },
        financial_research: { summary: "财务", evidence_ids: ["ev_001"], assumption_ids: ["asm_001"], risks: [] },
        valuation_research: { summary: "估值", evidence_ids: ["ev_001"], assumption_ids: ["asm_001"], risks: [] },
        lead_final_review: { key_findings: ["发现"], conflicts: [] },
      },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const output = messages.find((value) => value.id === "run-writer" && value.type === "response");
  assert.ok(output);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 0);
  assert.match(String(output.payload.output), /fundamental|executive_summary|品牌基础/);
  assert.match(String(output.payload.output), /asm_001/);
});

test("parallel writer mocks plan assigned topics and emit scoped section output", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => messages.push(value), "mock");
  bridge.handle(request("create-plan", "create_session", {
    session_id: "writer-plan-session",
    profile: { ...profile("constrained"), profile_id: "writer_planning" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-plan", "run_agent", {
    session_id: "writer-plan-session",
    context: {
      node: "writer_planning",
      run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" },
      artifacts: { lead_synthesis: { sections: [
        { section: "business", main_point: "业务质量", allowed_evidence_ids: ["ev_001"], allowed_assumption_ids: [] },
        { section: "industry", main_point: "行业周期", allowed_evidence_ids: ["ev_002"], allowed_assumption_ids: [] },
        { section: "financial", main_point: "财务质量", allowed_evidence_ids: ["ev_001"], allowed_assumption_ids: ["asm_001"] },
        { section: "valuation", main_point: "估值", allowed_evidence_ids: ["ev_001"], allowed_assumption_ids: ["asm_001"] },
      ] } },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const planned = messages.find((value) => value.id === "run-plan" && value.type === "response");
  assert.ok(planned);
  const plan = JSON.parse(String(planned.payload.output));
  assert.equal(plan.report_composition.length, 4);
  assert.deepEqual(plan.report_composition.map((item: Record<string, unknown>) => item.writer_group), [
    "business", "industry", "financial", "financial",
  ]);

  bridge.handle(request("create-section", "create_session", {
    session_id: "writer-section-session",
    profile: { ...profile("constrained"), profile_id: "writer_section" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-section", "run_agent", {
    session_id: "writer-section-session",
    context: {
      node: "writer_section_business", section_group: "business",
      run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" },
      artifacts: { writer_assignment: [plan.report_composition[0]] },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const written = messages.find((value) => value.id === "run-section" && value.type === "response");
  assert.ok(written);
  const section = JSON.parse(String(written.payload.output));
  assert.equal(section.section_group, "business");
  assert.equal(section.sections[0].section_id, "business-analysis");
});

test("final synthesis mock emits bounded edit instructions instead of rewritten sections", async () => {
  const messages: ProtocolMessage[] = [];
  const bridge = new Bridge((value) => messages.push(value), "mock");
  bridge.handle(request("create-synthesis", "create_session", {
    session_id: "final-synthesis-session",
    profile: { ...profile("constrained"), profile_id: "final_synthesis" },
    model: { runtime_mode: "mock" },
    tools: [],
  }));
  bridge.handle(request("run-synthesis", "run_agent", {
    session_id: "final-synthesis-session",
    context: {
      node: "final_synthesis",
      run: { resolved_symbol: "600519.SH", as_of: "2026-08-05" },
      artifacts: {
        lead_synthesis: { executive_focus: "业务、行业与财务联动" },
        writer_plan: { report_composition: [
          { section_id: "industry-analysis", narrative_order: 1 },
          { section_id: "business-analysis", narrative_order: 2 },
        ] },
        writer_sections: {
          business: { sections: [{ section_id: "business-analysis", body: "业务原稿" }] },
          industry: { sections: [{ section_id: "industry-analysis", body: "行业原稿" }] },
          financial: { sections: [{ section_id: "financial-analysis", body: "财务原稿" }] },
        },
      },
    },
    output_schema: {},
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const response = messages.find(
    (value) => value.id === "run-synthesis" && value.type === "response"
  );
  assert.ok(response);
  assert.equal(messages.filter((value) => value.type === "tool_call").length, 0);
  const output = JSON.parse(String(response.payload.output));
  assert.deepEqual(output.section_order, [
    "industry-analysis", "business-analysis", "financial-analysis",
  ]);
  assert.deepEqual(output.text_edits, []);
  assert.equal("sections" in output, false);
});

test("live usage summary aggregates provider tokens and reported cost", () => {
  const usage = summarizeUsage([
    { role: "assistant", usage: { input: 10, output: 4, totalTokens: 14, cost: { total: 0.01 } } },
    { role: "assistant", usage: { input: 7, output: 3, totalTokens: 10, cost: { total: 0.02 } } },
    { role: "user" },
  ]);

  assert.deepEqual(usage, {
    input_tokens: 17,
    output_tokens: 7,
    total_tokens: 24,
    estimated_cost: 0.03,
    cost_currency: "USD",
  });
});

test("live usage summary omits unknown provider cost", () => {
  const usage = summarizeUsage([
    { role: "assistant", usage: { input: 10, output: 4, totalTokens: 14 } },
  ]);

  assert.deepEqual(usage, {
    input_tokens: 10,
    output_tokens: 4,
    total_tokens: 14,
  });
});

test("live model preflight rejects an unknown provider model pair", () => {
  assert.throws(
    () => validateLiveModel("openai", "definitely-not-a-real-model"),
    /Unknown model/,
  );
});

test("live model preflight accepts the native DeepSeek pro model", () => {
  const { model } = validateLiveModel("deepseek", "deepseek-v4-pro");

  assert.equal(model.provider, "deepseek");
  assert.equal(model.id, "deepseek-v4-pro");
});
