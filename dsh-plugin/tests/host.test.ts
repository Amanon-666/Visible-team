import { Readable } from "node:stream";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { apply, type AgentDriver, type DshApiProxy } from "../src/host/index.js";
import type { ToolRuntimeLike } from "../src/host/model-tool.js";
import { API_PATH, CONTEXT_PATH, type TeamWorkspace, type WorkspaceCommandResult } from "../src/shared/types.js";

type Handler = (req: IncomingMessage, res: ServerResponse) => void | Promise<void>;
type ResponseCapture = ServerResponse & { body: string; headers: Record<string, string>; statusCode: number };

const cleanups: (() => void)[] = [];
const temporary: string[] = [];

function response(): ResponseCapture {
  const result = {
    body: "",
    headers: {},
    statusCode: 200,
    setHeader(name: string, value: string) { this.headers[name.toLowerCase()] = value; },
    write() { return true; },
    end(body?: string) { this.body = body ?? ""; },
  } as unknown as ResponseCapture;
  return result;
}

async function request(handler: Handler, method: string, url: string, body?: unknown): Promise<ResponseCapture> {
  const req = Readable.from(body === undefined ? [] : [JSON.stringify(body)]) as unknown as IncomingMessage;
  Object.assign(req, { method, url });
  const res = response();
  await handler(req, res);
  return res;
}

function payload<T>(res: ResponseCapture): T {
  return JSON.parse(res.body) as T;
}

interface SetupResult {
  routes: Map<string, Handler>;
  declaredInjections: string[][];
}

function setup(
  drivers: readonly AgentDriver[] = [],
  get?: (name: string) => unknown,
  apiProxy?: DshApiProxy,
  dsh?: { nativeProvider?: string },
  tools?: ToolRuntimeLike,
): SetupResult {
  const routes = new Map<string, Handler>();
  const declaredInjections: string[][] = [];
  const directory = mkdtempSync(join(tmpdir(), "visible-team-host-"));
  temporary.push(directory);
  const ctx = {
    webServer: {
      register(route: { path: string; handler: Handler }) {
        routes.set(route.path, route.handler);
        return () => { routes.delete(route.path); };
      },
    },
    get,
    inject(
      services: readonly ["apiProxy"] | readonly ["tools"],
      callback: (injected: { apiProxy?: DshApiProxy; tools?: ToolRuntimeLike; effect: (factory: () => (() => void) | void, name?: string) => void }) => void,
    ) {
      declaredInjections.push([...services]);
      if (services[0] === "apiProxy" && apiProxy !== undefined) callback({ apiProxy, effect: ctx.effect });
      if (services[0] === "tools" && tools !== undefined) callback({ tools, effect: ctx.effect });
      return { dispose() {} };
    },
    effect(factory: () => (() => void) | void) {
      const cleanup = factory();
      if (cleanup) cleanups.push(cleanup);
    },
  };
  apply(ctx, { statePath: join(directory, "state.sqlite"), drivers, dsh });
  return { routes, declaredInjections };
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
});

describe("Visible Team Host contract", () => {
  it("registers the model tool only through the public optional tools service", () => {
    const registered: unknown[] = [];
    const tools: ToolRuntimeLike = {
      register(definition) {
        registered.push(definition);
        return () => undefined;
      },
    };
    const result = setup([], undefined, undefined, undefined, tools);
    expect(result.declaredInjections).toContainEqual(["tools"]);
    expect(registered).toHaveLength(1);
    expect((registered[0] as { name: string }).name).toBe("visible_team");
  });

  it("routes a Leader model call through the existing driver/action executor", async () => {
    const sent: { agentId: string; content: string }[] = [];
    const driver: AgentDriver = {
      provider: "dsh",
      async send(input) {
        sent.push({ agentId: input.agent.agentId, content: input.content });
        return { accepted: true };
      },
    };
    const registered: unknown[] = [];
    const tools: ToolRuntimeLike = {
      register(definition) {
        registered.push(definition);
        return () => undefined;
      },
    };
    const { routes } = setup([driver], undefined, undefined, undefined, tools);
    const create = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "create-workspace", title: "模型入口", objective: "复用已有 Host action",
    }));
    const workspaceId = create.workspaces[0]?.workspaceId as string;
    const leader = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "Leader", binding: { provider: "dsh", nativeSessionId: "host-leader" }, asLeader: true,
    }));
    const leaderId = leader.workspaces[0]?.agents.find(agent => agent.binding.nativeSessionId === "host-leader")?.agentId as string;
    const member = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "Member", binding: { provider: "dsh", nativeSessionId: "host-member" },
    }));
    const memberId = member.workspaces[0]?.agents.find(agent => agent.binding.nativeSessionId === "host-member")?.agentId as string;
    const definition = registered[0] as { execute(args: unknown, exec: unknown): Promise<unknown> };
    const result = await definition.execute({ operation: "send_message", agentId: memberId, content: "来自 Leader 工具" }, {
      agent: { id: "host-leader" },
    });
    expect(result).toMatchObject({ kind: "action", operation: "send_message", workspaceId, agentId: memberId, accepted: true });
    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual({ agentId: memberId, content: "来自 Leader 工具" });
    expect(leaderId).not.toBe(memberId);
  });

  it("keeps delivery target-scoped and reports unavailable creation without attaching a fake Agent", async () => {
    const delivered: { agentId: string; content: string; packetVersions?: number[] }[] = [];
    const driver: AgentDriver = {
      provider: "codex",
      async send(input) {
        delivered.push({ agentId: input.agent.agentId, content: input.content, packetVersions: input.packetVersions });
        return { accepted: true };
      },
    };
    const { routes } = setup([driver]);
    const created = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "create-workspace", title: "跨平台目标", objective: "共享一个明确目标",
    }));
    const workspaceId = created.workspaces[0]?.workspaceId as string;
    const first = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "Codex A", binding: { provider: "codex", nativeSessionId: "codex-a" },
    }));
    const firstId = first.workspaces[0]?.agents[0]?.agentId as string;
    const second = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "Codex B", binding: { provider: "codex", nativeSessionId: "codex-b" },
    }));
    const secondId = second.workspaces[0]?.agents.find(agent => agent.agentId !== firstId)?.agentId as string;

    const packet = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "add-context", workspaceId, summary: "仅发给 A", targets: [firstId],
    }));
    const version = packet.workspaces[0]?.context.find(item => item.summary === "仅发给 A")?.version as number;
    const deliveredResult = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "deliver-context", workspaceId, agentId: firstId,
    }));
    expect(deliveredResult.delivery).toMatchObject({ action: "deliver-context", agentId: firstId, accepted: true, deliveredVersions: [1, version] });
    expect(delivered).toHaveLength(1);
    expect(delivered[0]).toMatchObject({ agentId: firstId, packetVersions: [1, version] });
    expect(delivered[0]?.content).toContain("共享一个明确目标");
    expect(delivered[0]?.content).toContain("仅发给 A");
    expect(delivered[0]?.content).not.toContain(secondId);

    const context = payload<{ context: unknown[] }>(await request(
      routes.get(CONTEXT_PATH) as Handler,
      "GET",
      `${CONTEXT_PATH}?workspace=${encodeURIComponent(workspaceId)}&agent=${encodeURIComponent(secondId)}`,
    ));
    expect(context.context).toHaveLength(1);
    expect(context.context[0]).toMatchObject({ targets: [secondId], deliveredAt: null, sourceRef: "visible-team:workspace-bootstrap" });

    const unavailable = await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "create-agent", workspaceId, displayName: "Claude new", provider: "claude-code",
    });
    expect(unavailable.statusCode).toBe(409);
    expect(payload(unavailable)).toMatchObject({ code: "capability-unavailable", capability: "agent.create", provider: "claude-code" });
    const afterUnavailable = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "GET", `${API_PATH}?workspace=${workspaceId}`));
    expect(afterUnavailable.workspaces[0]?.agents).toHaveLength(2);
  });

  it("uses the DSH adapter only when a public ApiProxy prompt face is present", async () => {
    const calls: { method: string; request: unknown }[] = [];
    const apiProxy: DshApiProxy = {
      sessions: {
        async list(request) {
          calls.push({ method: "list", request });
          return { result: { ok: true, value: { items: [{ sessionId: "session-current", running: true, blank: false }] } } };
        },
        async selectModel(request) {
          calls.push({ method: "selectModel", request });
          return { result: { ok: true, value: { selected: request.payload } } };
        },
        async prompt(request: unknown) {
          calls.push({ method: "prompt", request });
          return { result: { ok: true, value: { accepted: true } } };
        },
      },
    };
    const { routes, declaredInjections } = setup([], name => {
      if (name === "apiProxy") throw new Error("apiProxy must arrive through declared injection");
      return undefined;
    }, apiProxy, { nativeProvider: "deepseek-official" });
    expect(declaredInjections).toContainEqual(["apiProxy"]);
    const created = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "create-workspace", title: "DSH", objective: "发送一条明确指令",
    }));
    const workspaceId = created.workspaces[0]?.workspaceId as string;
    const attached = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "当前 Session", nativeProvider: "deepseek-official",
      model: "deepseek-chat", thinking: "high", permissionMode: "safe",
      binding: { provider: "dsh", nativeSessionId: "session-current" },
    }));
    const agentId = attached.workspaces[0]?.agents[0]?.agentId as string;
    const result = await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "send-agent", workspaceId, agentId, content: "请直接执行这条指令",
    });
    expect(result.statusCode).toBe(200);
    expect(calls.filter(call => call.method === "selectModel")).toHaveLength(1);
    expect(calls.filter(call => call.method === "prompt")).toHaveLength(1);
    expect(calls.find(call => call.method === "selectModel")).toMatchObject({ request: { payload: { sessionId: "session-current", provider: "deepseek-official", model: "deepseek-chat", reasoningEffort: "high" } } });
    expect(calls.find(call => call.method === "prompt")).toMatchObject({ request: { payload: { sessionId: "session-current", content: [{ type: "text", text: "请直接执行这条指令" }] } } });
    const afterDirect = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "GET", `${API_PATH}?workspace=${workspaceId}`));
    expect(afterDirect.workspaces[0]?.agents[0]?.pendingContext).toBe(1);
  });

  it("keeps existing DSH attachments usable without ApiProxy, but rejects sends explicitly", async () => {
    const { routes, declaredInjections } = setup();
    expect(declaredInjections).toContainEqual(["apiProxy"]);
    const created = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "create-workspace", title: "无网关", objective: "仍可保存挂接关系",
    }));
    const workspaceId = created.workspaces[0]?.workspaceId as string;
    const attached = payload<WorkspaceCommandResult>(await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "attach-agent", workspaceId, displayName: "当前 Session", binding: { provider: "dsh", nativeSessionId: "session-without-proxy" },
    }));
    const agentId = attached.workspaces[0]?.agents[0]?.agentId as string;
    expect(attached.workspaces[0]?.agents[0]?.pendingContext).toBe(1);
    const unavailable = await request(routes.get(API_PATH) as Handler, "POST", API_PATH, {
      action: "send-agent", workspaceId, agentId, content: "这条消息不应被静默丢弃",
    });
    expect(unavailable.statusCode).toBe(409);
    expect(payload(unavailable)).toMatchObject({ code: "capability-unavailable", capability: "agent.send", provider: "dsh" });
  });
});
