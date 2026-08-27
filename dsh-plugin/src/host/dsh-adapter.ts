import { randomUUID } from "node:crypto";
import type { AgentDriverResult, TeamAgent } from "../shared/types.js";

/**
 * Small public ApiProxy shape used by the DSH adapter. Keeping this structural
 * avoids importing a DSH source path and leaves the core independent of DSH.
 */
export interface DshApiProxy {
  sessions?: {
    prompt?: (request: {
      rpcId: string;
      payload: {
        sessionId: string;
        mode: "queue";
        content: [{ type: "text"; text: string }];
      };
    }) => Promise<unknown>;
  };
}

export function createDshDriver(apiProxy: DshApiProxy | undefined): {
  provider: "dsh";
  send(input: { agent: TeamAgent; content: string }): Promise<AgentDriverResult>;
} | undefined {
  const prompt = apiProxy?.sessions?.prompt;
  if (typeof prompt !== "function") return undefined;
  return {
    provider: "dsh",
    async send({ agent, content }) {
      const response = await prompt.call(apiProxy?.sessions, {
        rpcId: randomUUID(),
        payload: {
          sessionId: agent.binding.nativeSessionId,
          mode: "queue",
          content: [{ type: "text", text: content }],
        },
      }) as {
        result?: { ok?: boolean; error?: { message?: string } };
      };
      if (response?.result?.ok === false) {
        throw new Error(response.result.error?.message || "DSH session rejected the prompt");
      }
      if (response?.result?.ok !== true) {
        throw new Error("DSH apiProxy.sessions.prompt returned an invalid response");
      }
      return { accepted: true };
    },
  };
}
