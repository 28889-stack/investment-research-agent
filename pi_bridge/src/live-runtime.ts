import { Agent, type AgentTool } from "@earendil-works/pi-agent-core";
import { createModels, Type, type AssistantMessage, type Model } from "@earendil-works/pi-ai";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";

import type { SessionRecord } from "./protocol.js";

export type ToolInvoker = (
  name: string,
  arguments_: Record<string, unknown>,
) => Promise<Record<string, unknown>>;

const TECHNICAL_TOOL_ORDER = [
  "get_market_data",
  "calculate_technical_indicators",
  "get_technical_summary",
] as const;

interface TechnicalToolOutcome {
  result: Record<string, unknown>;
  reused: boolean;
  complete: boolean;
}

export class TechnicalToolSequence {
  private nextIndex = 0;
  private readonly completed = new Map<string, Record<string, unknown>>();

  get availableToolNames(): string[] {
    const next = TECHNICAL_TOOL_ORDER[this.nextIndex];
    return next ? [next] : [];
  }

  async execute(
    name: string,
    arguments_: Record<string, unknown>,
    invoke: ToolInvoker,
  ): Promise<TechnicalToolOutcome> {
    const cached = this.completed.get(name);
    if (cached) {
      return {
        result: cached,
        reused: true,
        complete: this.nextIndex === TECHNICAL_TOOL_ORDER.length,
      };
    }

    const expected = TECHNICAL_TOOL_ORDER[this.nextIndex];
    if (!expected) {
      throw new Error("Technical tool sequence is already complete");
    }
    if (name !== expected) {
      throw new Error(`Technical tool sequence expected ${expected}, received ${name}`);
    }

    const result = await invoke(name, arguments_);
    this.completed.set(name, result);
    this.nextIndex += 1;
    return {
      result,
      reused: false,
      complete: this.nextIndex === TECHNICAL_TOOL_ORDER.length,
    };
  }
}

export function validateLiveModel(provider: string, name: string) {
  const models = createModels();
  if (provider === "openai") {
    models.setProvider(openaiProvider());
  } else if (provider === "anthropic") {
    models.setProvider(anthropicProvider());
  } else if (provider === "deepseek") {
    models.setProvider(deepseekProvider());
  } else {
    throw new Error(`Unsupported live provider: ${provider}`);
  }
  const model = models.getModel(provider, name);
  if (!model) {
    throw new Error(`Unknown model ${provider}/${name}`);
  }
  return { models, model };
}

function configuredModels(session: SessionRecord) {
  return validateLiveModel(
    String(session.model.provider ?? ""),
    String(session.model.name ?? ""),
  );
}

function bridgeTools(
  session: SessionRecord,
  invoke: () => ToolInvoker,
  technicalSequence?: TechnicalToolSequence,
): AgentTool[] {
  return session.tools.map((tool) => ({
    name: tool.name,
    label: tool.name,
    description: tool.description,
    executionMode: technicalSequence ? "sequential" as const : undefined,
    parameters: Type.Unsafe<Record<string, unknown>>(tool.input_schema),
    execute: async (_toolCallId, params) => {
      if (typeof params !== "object" || params === null || Array.isArray(params)) {
        throw new Error(`Invalid tool arguments for ${tool.name}`);
      }
      const outcome = technicalSequence
        ? await technicalSequence.execute(
            tool.name,
            params as Record<string, unknown>,
            invoke(),
          )
        : undefined;
      const result = outcome?.result ?? await invoke()(
        tool.name,
        params as Record<string, unknown>,
      );
      const content = [{ type: "text" as const, text: JSON.stringify(result) }];
      if (outcome?.complete) {
        content.push({
          type: "text" as const,
          text: "\n三个必需技术工具已全部成功完成。工具阶段已结束，请立即根据已返回的结果生成符合 Schema 的最终 JSON。",
        });
      } else if (outcome?.reused) {
        content.push({
          type: "text" as const,
          text: "\n该工具已成功执行，本次直接复用已校验结果，请继续下一个当前可用工具。",
        });
      }
      return {
        content,
        details: { bridge: "python_tool_registry" },
      };
    },
  }));
}

function finalText(messages: readonly unknown[]): string {
  const assistant = [...messages]
    .reverse()
    .find((candidate): candidate is AssistantMessage => {
      return (
        typeof candidate === "object" &&
        candidate !== null &&
        (candidate as { role?: string }).role === "assistant"
      );
    });
  if (!assistant) throw new Error("Pi Agent returned no assistant message");
  if (assistant.stopReason === "error" || assistant.stopReason === "aborted") {
    throw new Error(assistant.errorMessage ?? `Pi Agent stopped: ${assistant.stopReason}`);
  }
  const text = assistant.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
  if (!text.trim()) throw new Error("Pi Agent returned no final text");
  return text;
}

export interface UsageSummary {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost?: number;
  cost_currency?: "USD";
}

export function summarizeUsage(messages: readonly unknown[]): UsageSummary | undefined {
  const usages = messages.flatMap((message) => {
    if (typeof message !== "object" || message === null || (message as { role?: string }).role !== "assistant") {
      return [];
    }
    const usage = (message as { usage?: Record<string, unknown> }).usage;
    return usage && typeof usage === "object" ? [usage] : [];
  });
  if (usages.length === 0) return undefined;
  const summary: UsageSummary = {
    input_tokens: usages.reduce((sum, usage) => sum + Number(usage.input ?? 0), 0),
    output_tokens: usages.reduce((sum, usage) => sum + Number(usage.output ?? 0), 0),
    total_tokens: usages.reduce((sum, usage) => sum + Number(usage.totalTokens ?? 0), 0),
  };
  const reportedCosts = usages.flatMap((usage) => {
    const value = (usage.cost as { total?: unknown } | undefined)?.total;
    return typeof value === "number" && Number.isFinite(value) && value >= 0
      ? [value]
      : [];
  });
  if (reportedCosts.length > 0) {
    summary.estimated_cost = reportedCosts.reduce((sum, value) => sum + value, 0);
    summary.cost_currency = "USD";
  }
  return summary;
}

export interface LiveRunResult {
  output: string;
  usage?: UsageSummary;
}

export class LiveAgentSession {
  private readonly agent: Agent;
  private activeInvoker: ToolInvoker | undefined;
  private turns = 0;

  constructor(private readonly session: SessionRecord, systemPrompt: string) {
    const { models, model } = configuredModels(session);
    const technicalSequence = session.profile.profile_id === "technical_research"
      ? new TechnicalToolSequence()
      : undefined;
    const allTools = bridgeTools(
      session,
      () => {
        if (!this.activeInvoker) throw new Error("Tool callback is unavailable");
        return this.activeInvoker;
      },
      technicalSequence,
    );
    const currentTechnicalTools = () => {
      if (!technicalSequence) return allTools;
      const permitted = new Set(technicalSequence.availableToolNames);
      return allTools.filter((tool) => permitted.has(tool.name));
    };
    this.agent = new Agent({
      sessionId: session.sessionId,
      streamFn: models.streamSimple.bind(models),
      initialState: {
        systemPrompt,
        model: model as Model<any>,
        thinkingLevel: "medium",
        tools: currentTechnicalTools(),
      },
      prepareNextTurnWithContext: technicalSequence
        ? ({ context }) => {
            const tools = currentTechnicalTools();
            this.agent.state.tools = tools;
            return {
              context: {
                ...context,
                tools,
              },
            };
          }
        : undefined,
    });
    this.agent.subscribe((event) => {
      if (event.type === "turn_start") {
        this.turns += 1;
        if (this.turns > this.session.profile.max_iterations) {
          this.agent.abort();
        }
      }
    });
  }

  async run(
    context: Record<string, unknown>,
    task: string,
    outputSchema: Record<string, unknown>,
    invoke: ToolInvoker,
  ): Promise<LiveRunResult> {
    this.turns = 0;
    this.activeInvoker = invoke;
    const messageStart = this.agent.state.messages.length;
    try {
      await this.agent.prompt(
        [
          task,
          "\n受控上下文：",
          JSON.stringify(context),
          "\n输出 JSON Schema：",
          JSON.stringify(outputSchema),
          "\n只返回符合 Schema 的 JSON，不要返回思考过程。",
        ].join("\n"),
      );
      const messages = this.agent.state.messages.slice(messageStart);
      return { output: finalText(messages), usage: summarizeUsage(messages) };
    } finally {
      this.activeInvoker = undefined;
    }
  }

  async repair(
    rawOutput: string,
    validationError: string,
    outputSchema: Record<string, unknown>,
  ): Promise<LiveRunResult> {
    const tools = this.agent.state.tools;
    this.agent.state.tools = [];
    this.turns = 0;
    const messageStart = this.agent.state.messages.length;
    try {
      await this.agent.prompt(
        [
          "上一次输出未通过 Schema 校验。",
          `校验错误：${validationError}`,
          `原输出：${rawOutput}`,
          `JSON Schema：${JSON.stringify(outputSchema)}`,
          "只返回修复后的 JSON，不得调用工具或输出思考过程。",
        ].join("\n"),
      );
      const messages = this.agent.state.messages.slice(messageStart);
      return { output: finalText(messages), usage: summarizeUsage(messages) };
    } finally {
      this.agent.state.tools = tools;
    }
  }

  close(): void {
    this.agent.abort();
    this.agent.reset();
    this.activeInvoker = undefined;
  }
}
