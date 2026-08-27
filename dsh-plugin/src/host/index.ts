import type { IncomingMessage, ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { API_PATH, CONTEXT_PATH, EVENTS_PATH, type AgentDriverResult, type CapabilityUnavailable, type ContextPacket, type TeamAgent, type WorkspaceAction, type WorkspaceCommandResult, type WorkspaceStateAction } from "../shared/types.js";
import { readJson, sendJson } from "./http.js";
import { createDshDriver, type DshApiProxy } from "./dsh-adapter.js";
import { VisibleTeamStore, type StoreConfig } from "./store.js";

type WebServer = {
  register(route: {
    kind: "exact" | "prefix";
    path: string;
    handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>;
  }): () => void;
};

type InjectionHandle = { dispose?: () => void } | void;

type HostContext = {
  webServer: WebServer;
  get?: (name: string) => unknown;
  /**
   * Cordis' optional-service binding. The callback receives the declared
   * service as a property; this is intentionally separate from `get()` so a
   * DSH driver cannot accidentally capture an undeclared service.
   */
  inject?: (
    services: readonly ["apiProxy"],
    callback: (ctx: HostContext & { apiProxy: DshApiProxy }) => void,
  ) => InjectionHandle;
  effect?: (factory: () => (() => void) | void, name?: string) => void;
};

export interface DriverInput {
  workspaceId: string;
  agent: TeamAgent;
  content: string;
  packetVersions?: number[];
}

/** Optional provider seam. A driver owns transport; the core owns identity/state. */
export interface AgentDriver {
  provider: string;
  send(input: DriverInput): Promise<AgentDriverResult>;
  create?(input: {
    workspaceId: string;
    displayName: string;
    model?: string;
    thinking?: string;
    permissionMode?: string;
    responsibility?: string;
  }): Promise<{
    nativeSessionId: string;
    nativeOpenRef?: string;
  }>;
}

export interface Config extends StoreConfig {
  /** Test/embedding seam; ordinary package installs have no external driver by default. */
  drivers?: readonly AgentDriver[];
}

export const name = "visible-team";
export const inject = ["webServer"];

export class CapabilityUnavailableError extends Error implements CapabilityUnavailable {
  readonly code = "capability-unavailable" as const;
  constructor(
    readonly capability: string,
    message: string,
    readonly provider?: string,
  ) {
    super(message);
    this.name = "CapabilityUnavailableError";
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function unavailablePayload(error: CapabilityUnavailableError): CapabilityUnavailable {
  return {
    code: error.code,
    capability: error.capability,
    ...(error.provider === undefined ? {} : { provider: error.provider }),
    message: error.message,
  };
}

function asAction(value: unknown): WorkspaceAction {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("action must be an object");
  const action = (value as Record<string, unknown>).action;
  if (typeof action !== "string" || !action) throw new Error("action is required");
  return value as WorkspaceAction;
}

function contentText(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) throw new Error("content is required");
  const content = value.trim();
  if (content.length > 20_000) throw new Error("content is too long");
  return content;
}

function throughVersion(value: unknown): number | undefined {
  if (value === undefined) return undefined;
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error("throughVersion must be a non-negative integer");
  return Number(value);
}

function driverRegistry(ctx: HostContext, config: Config): Map<string, AgentDriver> {
  const result = new Map<string, AgentDriver>();
  const add = (driver: AgentDriver): void => {
    if (!driver || typeof driver.provider !== "string" || !driver.provider.trim() || typeof driver.send !== "function") {
      throw new Error("visible-team driver must provide a provider and send()");
    }
    if (result.has(driver.provider)) throw new Error(`duplicate Visible Team driver: ${driver.provider}`);
    result.set(driver.provider, driver);
  };
  for (const driver of config.drivers ?? []) add(driver);
  const supplied = ctx.get?.("visibleTeam.drivers");
  if (Array.isArray(supplied)) for (const driver of supplied as AgentDriver[]) add(driver);
  return result;
}

function usageAction(workspaceId: string, agentId: string, usage: NonNullable<AgentDriverResult["usage"]>): Extract<WorkspaceStateAction, { action: "record-usage" }> {
  const optional = (value: number | null): number | undefined => value === null ? undefined : value;
  return {
    action: "record-usage",
    workspaceId,
    agentId,
    observationId: usage.observationId ?? randomUUID(),
    source: usage.source,
    accounting: usage.accounting,
    observedAt: usage.observedAt,
    inputTokens: optional(usage.inputTokens),
    cachedInputTokens: optional(usage.cachedInputTokens),
    outputTokens: optional(usage.outputTokens),
    reasoningOutputTokens: optional(usage.reasoningOutputTokens),
    totalTokens: optional(usage.totalTokens),
  };
}

function contextMessage(packets: readonly ContextPacket[]): string {
  return packets.map(packet => {
    const source = packet.sourceRef === null ? "" : ` [${packet.sourceRef}]`;
    return `[Visible Team context v${packet.version}]${source}\n${packet.summary}`;
  }).join("\n\n");
}

async function executeAction(
  ctx: HostContext,
  store: VisibleTeamStore,
  drivers: Map<string, AgentDriver>,
  action: WorkspaceAction,
): Promise<WorkspaceCommandResult> {
  if (isStateAction(action)) {
    const workspace = store.dispatch(action);
    return { workspaces: [workspace] };
  }

  if (action.action === "create-agent") {
    const workspace = store.snapshot(action.workspaceId);
    const driver = drivers.get(action.provider);
    if (driver?.create === undefined) {
      throw new CapabilityUnavailableError(
        "agent.create",
        `provider ${action.provider} has no create driver; attach an existing native session instead`,
        action.provider,
      );
    }
    const created = await driver.create({
      workspaceId: action.workspaceId,
      displayName: action.displayName,
      model: action.model,
      thinking: action.thinking,
      permissionMode: action.permissionMode,
      responsibility: action.responsibility,
    });
    if (!created || typeof created.nativeSessionId !== "string" || !created.nativeSessionId.trim()) {
      throw new Error(`driver ${action.provider} did not return a native session id`);
    }
    const next = store.attachCreatedAgent({
      workspaceId: workspace.workspaceId,
      displayName: action.displayName,
      provider: action.provider,
      nativeSessionId: created.nativeSessionId,
      nativeOpenRef: created.nativeOpenRef,
      model: action.model,
      thinking: action.thinking,
      permissionMode: action.permissionMode,
      responsibility: action.responsibility,
      asLeader: action.asLeader,
    });
    return { workspaces: [next] };
  }

  const workspace = store.snapshot(action.workspaceId);
  const agent = store.getAgent(action.workspaceId, action.agentId);
  const driver = drivers.get(agent.binding.provider);
  if (driver === undefined) {
    throw new CapabilityUnavailableError(
      "agent.send",
      `no driver is installed for provider ${agent.binding.provider}; the native session remains attached but was not contacted`,
      agent.binding.provider,
    );
  }

  if (action.action === "send-agent") {
    const result = await driver.send({
      workspaceId: action.workspaceId,
      agent,
      content: contentText(action.content),
    });
    if (result.usage !== undefined) store.dispatch(usageAction(action.workspaceId, action.agentId, result.usage));
    return {
      workspaces: [store.snapshot(action.workspaceId)],
      delivery: { action: "send-agent", agentId: action.agentId, accepted: result.accepted },
    };
  }

  const limit = throughVersion(action.throughVersion);
  const packets = store.contextForAgent(action.workspaceId, action.agentId, true, limit);
  if (packets.length === 0) {
    return {
      workspaces: [workspace],
      delivery: { action: "deliver-context", agentId: action.agentId, accepted: false, deliveredVersions: [], reason: "no-pending-context" },
    };
  }
  const result = await driver.send({
    workspaceId: action.workspaceId,
    agent,
    content: contextMessage(packets),
    packetVersions: packets.map(packet => packet.version),
  });
  if (result.usage !== undefined) store.dispatch(usageAction(action.workspaceId, action.agentId, result.usage));
  store.dispatch({
    action: "ack-context",
    workspaceId: action.workspaceId,
    agentId: action.agentId,
    throughVersion: packets[packets.length - 1]?.version ?? 0,
  });
  return {
    workspaces: [store.snapshot(action.workspaceId)],
    delivery: {
      action: "deliver-context",
      agentId: action.agentId,
      accepted: result.accepted,
      deliveredVersions: packets.map(packet => packet.version),
    },
  };
}

function isStateAction(action: WorkspaceAction): action is WorkspaceStateAction {
  return action.action !== "create-agent" && action.action !== "deliver-context" && action.action !== "send-agent";
}

export function apply(ctx: HostContext, config: Config = {}): void {
  const store = new VisibleTeamStore(config);
  const drivers = driverRegistry(ctx, config);
  const hasConfiguredDshDriver = drivers.has("dsh");
  let injectedDshDriver: AgentDriver | undefined;
  // apiProxy is optional for the plugin as a whole: attaching an existing
  // native Session must still work on a host without the gateway. The child
  // injection is the public Cordis declaration for the DSH-only capability.
  const apiProxyFiber = ctx.inject?.(["apiProxy"], (apiCtx) => {
    if (hasConfiguredDshDriver) return;
    const dsh = createDshDriver(apiCtx.apiProxy);
    if (dsh === undefined) return;
    injectedDshDriver = dsh;
    drivers.set(dsh.provider, dsh);
    apiCtx.effect?.(() => () => {
      if (injectedDshDriver !== dsh) return;
      injectedDshDriver = undefined;
      drivers.delete(dsh.provider);
    }, "visible-team: optional DSH apiProxy driver");
  });
  const clients = new Set<ServerResponse>();
  const routeDisposers: (() => void)[] = [];
  const broadcast = (workspaceId?: string): void => {
    const payload = JSON.stringify({ workspaceId: workspaceId ?? null });
    for (const client of [...clients]) {
      try { client.write(`event: change\ndata: ${payload}\n\n`); } catch { clients.delete(client); }
    }
  };

  routeDisposers.push(ctx.webServer.register({
    kind: "exact",
    path: API_PATH,
    handler: async (req, res) => {
      if (req.method === "GET" || req.method === undefined) {
        const url = new URL(req.url ?? API_PATH, "http://dsh.local");
        const workspaceId = url.searchParams.get("workspace");
        try {
          return sendJson(res, 200, {
            workspaces: workspaceId ? [store.snapshot(workspaceId)] : store.listWorkspaces(),
          });
        } catch (error) {
          return sendJson(res, 404, { error: message(error) });
        }
      }
      if (req.method !== "POST") return sendJson(res, 405, { error: "method not allowed" });
      try {
        const action = asAction(await readJson(req));
        const result = await executeAction(ctx, store, drivers, action);
        broadcast(result.workspaces[0]?.workspaceId);
        return sendJson(res, 200, result);
      } catch (error) {
        if (error instanceof CapabilityUnavailableError) return sendJson(res, 409, unavailablePayload(error));
        return sendJson(res, 400, { error: message(error) });
      }
    },
  }));

  routeDisposers.push(ctx.webServer.register({
    kind: "exact",
    path: CONTEXT_PATH,
    handler: (req, res) => {
      if (req.method !== "GET" && req.method !== undefined) return sendJson(res, 405, { error: "method not allowed" });
      const url = new URL(req.url ?? CONTEXT_PATH, "http://dsh.local");
      const workspaceId = url.searchParams.get("workspace");
      const agentId = url.searchParams.get("agent");
      if (!workspaceId || !agentId) return sendJson(res, 400, { error: "workspace and agent are required; context is never broadcast" });
      try {
        const pendingOnly = url.searchParams.get("pending") !== "false";
        const through = url.searchParams.get("through");
        const packets = store.contextForAgent(
          workspaceId,
          agentId,
          pendingOnly,
          through === null ? undefined : Number(through),
        );
        return sendJson(res, 200, { workspaceId, agentId, context: packets });
      } catch (error) {
        return sendJson(res, 404, { error: message(error) });
      }
    },
  }));

  routeDisposers.push(ctx.webServer.register({
    kind: "exact",
    path: EVENTS_PATH,
    handler: (req, res) => {
      res.statusCode = 200;
      res.setHeader("content-type", "text/event-stream; charset=utf-8");
      res.setHeader("cache-control", "no-cache");
      res.setHeader("connection", "keep-alive");
      res.write(":\n\n");
      clients.add(res);
      req.on("close", () => clients.delete(res));
    },
  }));

  const dispose = (): void => {
    apiProxyFiber?.dispose?.();
    for (const disposeRoute of routeDisposers.splice(0)) disposeRoute();
    for (const client of clients) client.end();
    clients.clear();
    store.close();
  };
  if (ctx.effect) ctx.effect(() => dispose, "visible-team: state, drivers, and event stream");
}

export { VisibleTeamStore } from "./store.js";
export type { DshApiProxy } from "./dsh-adapter.js";
