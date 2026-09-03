export interface ProtocolMessage {
  id: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface ProfilePayload {
  profile_id: string;
  version: string;
  mode: "full" | "constrained";
  max_iterations: number;
  max_tool_calls: number;
  [key: string]: unknown;
}

export interface ToolDescriptor {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

export interface SessionRecord {
  sessionId: string;
  profile: ProfilePayload;
  model: {
    provider?: string | null;
    name?: string | null;
    runtime_mode?: string;
  };
  tools: ToolDescriptor[];
}

export function parseMessage(line: string): ProtocolMessage {
  const parsed: unknown = JSON.parse(line);
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    typeof (parsed as ProtocolMessage).id !== "string" ||
    typeof (parsed as ProtocolMessage).type !== "string" ||
    typeof (parsed as ProtocolMessage).payload !== "object" ||
    (parsed as ProtocolMessage).payload === null
  ) {
    throw new Error("Invalid protocol message");
  }
  return parsed as ProtocolMessage;
}

export function message(
  id: string,
  type: string,
  payload: Record<string, unknown>,
): ProtocolMessage {
  return { id, type, payload };
}
