import { describe, expect, it } from "vitest";
import type { TeamAgent } from "../src/shared/types.js";
import {
  createDshDriver,
  type DshApiProxy,
  type DshRpcResponse,
} from "../src/host/dsh-adapter.js";

const route = {
  provider: "deepseek-official",
  model: "deepseek-chat",
  thinking: "high",
  permissions: { mode: "safe", workspace: "/tmp/visible-team-contract" },
} as const;

function ok(value: unknown): DshRpcResponse {
  return { result: { ok: true, value } };
}

function agent(overrides: Partial<TeamAgent> = {}): TeamAgent {
  return {
    agentId: "agent-1",
    workspaceId: "visible-workspace",
    displayName: "DSH agent",
    binding: {
      provider: "dsh",
      nativeProvider: "deepseek-official",
      nativeSessionId: "session-1",
      nativeOpenRef: null,
    },
    model: "deepseek-chat",
    thinking: "high",
    permissionMode: "safe",
    responsibility: "test",
    status: "active",
    attachSource: "manual",
    contextVersion: 0,
    pendingContext: 0,
    usage: {
      inputTokens: null,
      cachedInputTokens: null,
      outputTokens: null,
      reasoningOutputTokens: null,
      totalTokens: null,
      accounting: null,
      latestObservationId: null,
    },
    createdAt: "2026-08-28T00:00:00.000Z",
    updatedAt: "2026-08-28T00:00:00.000Z",
    ...overrides,
  };
}

interface FakeState {
  calls: { method: string; request: unknown }[];
  historyValue: unknown;
}

function fakeProxy(state: FakeState): DshApiProxy {
  return {
    sessions: {
      async list(request) {
        state.calls.push({ method: "list", request });
        return ok({ items: [{ sessionId: "session-1", running: true, blank: false, updatedAt: 42 }] });
      },
      async create(request) {
        state.calls.push({ method: "create", request });
        return ok({ sessionId: "session-created" });
      },
      async selectModel(request) {
        state.calls.push({ method: "selectModel", request });
        return ok({ selected: { provider: request.payload.provider, model: request.payload.model, reasoningEffort: request.payload.reasoningEffort } });
      },
      async prompt(request) {
        state.calls.push({ method: "prompt", request });
        return ok({ accepted: true });
      },
      async history(request) {
        state.calls.push({ method: "history", request });
        return ok(state.historyValue);
      },
    },
    events: {
      async mux(request) {
        state.calls.push({ method: "mux", request });
        return (async function* () {
          yield { payload: { type: "session/event", sessionId: "other", event: { type: "turn/start", seq: 1 } } };
          yield { payload: { type: "session/subscribed", sessionId: "session-1", lastSeq: 4 } };
          yield { payload: { type: "session/event", sessionId: "session-1", event: { type: "turn/start", seq: 5 } } };
          yield {
            payload: {
              type: "session/event",
              sessionId: "session-1",
              event: {
                type: "assistant/chunk",
                seq: 6,
                data: {
                  turn: 1,
                  step: 1,
                  chunk: { type: "usage", usage: { inputTokens: 11, cacheReadTokens: 2, outputTokens: 4 } },
                },
              },
            },
          };
          yield { payload: { type: "session/event", sessionId: "session-1", event: { type: "turn/end", seq: 7 } } };
        })();
      },
    },
  };
}

describe("DSH AgentDriver contract", () => {
  it("discovers the public capability matrix without probing a model or quota", () => {
    const state: FakeState = { calls: [], historyValue: { events: [], hasMore: false } };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    expect(driver).toBeDefined();
    expect(driver?.discover?.()).toMatchObject({
      provider: "dsh",
      available: true,
      configuration: { nativeProvider: route.provider, quotaProbe: false, routeRequired: true },
      capabilities: {
        discover: { status: "available" },
        attach: { status: "available" },
        create: { status: "available" },
        send: { status: "available" },
        resume: { status: "available" },
        status: { status: "available" },
        watch: { status: "available" },
        open: { status: "unavailable" },
        usage: { status: "available" },
      },
    });
    expect(state.calls).toHaveLength(0);
  });

  it("attaches only a listed native session and creates with an explicit route", async () => {
    const state: FakeState = { calls: [], historyValue: { events: [], hasMore: false } };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    const attached = await driver?.attach?.({
      workspaceId: "visible-workspace",
      displayName: "attached",
      nativeSessionId: "session-1",
      nativeOpenRef: "dsh://session-1",
      route,
    });
    expect(attached).toMatchObject({ nativeSessionId: "session-1", nativeOpenRef: "dsh://session-1", evidence: { source: "dsh.sessions.list", running: true } });

    const created = await driver?.create?.({
      workspaceId: "visible-workspace",
      displayName: "created",
      nativeProvider: route.provider,
      model: route.model,
      thinking: route.thinking,
      permissionMode: "safe",
      permissions: route.permissions,
      responsibility: "offline contract test",
    });
    expect(created).toMatchObject({
      nativeSessionId: "session-created",
      nativeOpenRef: null,
      evidence: {
        source: "dsh.sessions.create+selectModel",
        nativeProvider: route.provider,
        model: route.model,
        thinking: route.thinking,
        permissionMode: "safe",
        permissionsForwarded: false,
      },
    });
    expect(state.calls.filter(call => call.method === "create")[0]?.request).toMatchObject({ payload: { cwd: "/tmp/visible-team-contract" } });
    expect(state.calls.filter(call => call.method === "selectModel").at(-1)?.request).toMatchObject({
      payload: { sessionId: "session-created", provider: route.provider, model: route.model, reasoningEffort: route.thinking },
    });
  });

  it("selects the explicit route before send/resume and never estimates prompt usage", async () => {
    const state: FakeState = { calls: [], historyValue: { events: [], hasMore: false } };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    const input = { workspaceId: "visible-workspace", agent: agent(), content: "offline instruction", route };
    await expect(driver?.send(input)).resolves.toEqual({ accepted: true });
    await expect(driver?.resume?.({ ...input, content: "resume instruction" })).resolves.toEqual({ accepted: true });
    expect(state.calls.filter(call => call.method === "selectModel")).toHaveLength(2);
    expect(state.calls.filter(call => call.method === "prompt")).toHaveLength(2);
    for (const call of state.calls.filter(call => call.method === "prompt")) {
      expect(call.request).toMatchObject({ payload: { sessionId: "session-1", mode: "queue", content: [{ type: "text" }] } });
      expect((call.request as { payload: Record<string, unknown> }).payload).not.toHaveProperty("provider");
      expect((call.request as { payload: Record<string, unknown> }).payload).not.toHaveProperty("model");
      expect((call.request as { payload: Record<string, unknown> }).payload).not.toHaveProperty("permissions");
    }

    const { route: _route, ...withoutExplicitRoute } = input;
    await expect(driver?.send({ ...withoutExplicitRoute, agent: agent({ model: null }) })).rejects.toMatchObject({
      code: "capability-unavailable",
      capability: "agent.route",
      provider: "dsh",
    });
  });

  it("maps status and filters the native mux stream to one session", async () => {
    const state: FakeState = { calls: [], historyValue: { events: [], hasMore: false } };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    const status = await driver?.status?.({ agent: agent(), route });
    expect(status).toMatchObject({
      nativeSessionId: "session-1",
      taskExists: true,
      hostStatus: "running",
      resultAvailable: null,
      needsAttention: null,
      sourceRef: "dsh.sessions.list",
    });

    const events: unknown[] = [];
    if (driver?.watch === undefined) throw new Error("watch driver missing");
    for await (const event of driver.watch({ agent: agent(), route })) events.push(event);
    expect(events).toHaveLength(4);
    expect(events[0]).toMatchObject({ type: "status", status: "subscribed", nativeSessionId: "session-1" });
    expect(events[1]).toMatchObject({ type: "event", status: "running", event: { type: "turn/start" } });
    expect(events[2]).toMatchObject({
      type: "event",
      usage: {
        source: "dsh.session-event",
        accounting: "delta",
        inputTokens: 11,
        cachedInputTokens: 2,
        outputTokens: 4,
        totalTokens: null,
        observationId: "seq:6",
      },
    });
    expect(events[3]).toMatchObject({ type: "event", status: "idle", event: { type: "turn/end" } });
    expect(state.calls.find(call => call.method === "mux")?.request).toMatchObject({ payload: { since: { "session-1": 0 } } });
  });

  it("reads the official tokenUsage projection and preserves missing native totals", async () => {
    const state: FakeState = {
      calls: [],
      historyValue: {
        events: [],
        hasMore: false,
        projections: {
          asOfSeq: 10,
          values: {
            tokenUsage: { uncachedInputTokens: 20, cacheReadTokens: 3, outputTokens: 8 },
          },
        },
      },
    };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    const result = await driver?.usage?.({ agent: agent(), route });
    expect(result).toEqual({
      status: "available",
      nativeSessionId: "session-1",
      usage: {
        source: "dsh.session.projection.tokenUsage",
        accounting: "cumulative",
        inputTokens: 20,
        cachedInputTokens: 3,
        outputTokens: 8,
        reasoningOutputTokens: null,
        totalTokens: null,
        observationId: "seq:10",
      },
    });
    expect(result?.usage?.totalTokens).toBeNull();
  });

  it("folds native history samples without deriving a total and reports open as unavailable", async () => {
    const state: FakeState = {
      calls: [],
      historyValue: {
        events: [
          { event: { type: "assistant/chunk", seq: 2, data: { turn: 1, step: 1, chunk: { type: "usage", usage: { inputTokens: 5, outputTokens: 2 } } } } },
          { event: { type: "assistant/message", seq: 3, data: { turn: 1, step: 1, usage: { inputTokens: 5, outputTokens: 4 } } } },
        ],
        hasMore: false,
      },
    };
    const driver = createDshDriver(fakeProxy(state), { nativeProvider: route.provider });
    const result = await driver?.usage?.({ agent: agent(), route });
    expect(result).toMatchObject({ status: "available", nativeSessionId: "session-1" });
    expect(result?.usage).toMatchObject({
      source: "dsh.session-event.fold",
      accounting: "cumulative",
      inputTokens: 5,
      outputTokens: 4,
      totalTokens: null,
      observationId: "seq:3",
    });
    expect(await driver?.open?.({ agent: agent(), route })).toMatchObject({
      status: "unavailable",
      nativeSessionId: "session-1",
      nativeOpenRef: null,
    });
  });

  it("advertises and rejects missing public faces explicitly", async () => {
    const driver = createDshDriver({ sessions: { prompt: async () => ok({ accepted: true }) } });
    expect(driver?.discover?.().capabilities).toMatchObject({
      attach: { status: "unavailable" },
      create: { status: "unavailable" },
      send: { status: "unavailable" },
      resume: { status: "unavailable" },
      status: { status: "unavailable" },
      watch: { status: "unavailable" },
      open: { status: "unavailable" },
      usage: { status: "unavailable" },
    });
    const input = { workspaceId: "visible-workspace", agent: agent(), content: "must not send", route };
    await expect(driver?.send(input)).rejects.toMatchObject({ code: "capability-unavailable", capability: "agent.send" });
    await expect(driver?.status?.({ agent: agent(), route })).rejects.toMatchObject({ code: "capability-unavailable", capability: "agent.status" });
  });
});
