import { createInterface } from "node:readline";

import { Bridge } from "./bridge.js";
import { message, parseMessage } from "./protocol.js";

function send(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

const bridge = new Bridge(send);
send(message("bridge", "ready", { pid: process.pid }));

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", (line) => {
  if (!line.trim()) return;
  try {
    bridge.handle(parseMessage(line));
  } catch (error: unknown) {
    send(
      message("unknown", "error", {
        code: "BRIDGE_PROTOCOL_ERROR",
        message: error instanceof Error ? error.message : String(error),
      }),
    );
  }
});

lines.on("close", () => process.exit(0));
