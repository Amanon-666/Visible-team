import { randomUUID } from "node:crypto";
import type { AgentDriverResult, TeamAgent } from "../shared/types.js";
import {
  AgentDriverCapabilityError,
  explicitRouteForAgent,
  type AgentDriver,
  type AgentDriverAttachInput,
  type AgentDriverCapabilities,
  type AgentDriverCreateInput,
  type AgentDriverDiscovery,
  type AgentDriverObservation,
  type AgentDriverOpenResult,
  type AgentDriverRoute,
  type AgentDriverSessionInput,
  type AgentDriverUsageResult,
  type AgentDriverWatchEvent,
  type NativeUsageObservation,
} from "./driver.js";

/**
 * Structural public ApiProxy types. The adapter deliberately does not import
 * a DSH source path: hosts may provide the same public seam from a bundled
 * ApiProxy or from an offline fake in contract tests.
 */
export interface DshRpcRequest<P> {
  rpcId: string;
  payload: P;
}

export interface DshRpcError {
  code?: string;
  message?: string;
  details?: unknown;
}

export type DshRpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; error?: DshRpcError };

export interface DshRpcResponse<T = unknown> {
  rpcId?: string;
  result?: DshRpcResult<T>;
}

export interface DshPromptContentPart {
  type: "text";
  text: string;
}

export interface DshSessionSummary {
  sessionId: string;
  updatedAt?: number;
  running?: boolean;
  blank?: boolean;
  cwd?: string;
  origin?: string;
}

export interface DshHistoryEntry {
  event?: unknown;
  view?: unknown;
  [key: string]: unknown;
}

export interface DshSessionProjectionBlock {
  asOfSeq: number;
  values: Readonly<Record<string, unknown>>;
}

type DshRpcMethod<P> = (request: DshRpcRequest<P>) => Promise<unknown>;

export interface DshSessionsApi {
  list?: DshRpcMethod<{ cursor?: string }>;
  create?: DshRpcMethod<{ cwd?: string; sessionId?: string; agentPreset?: string }>;
  history?: DshRpcMethod<{ sessionId: string; beforeSeq?: number; maxMessages?: number }>;
  selectModel?: DshRpcMethod<{
    sessionId: string;
    provider: string;
    model: string;
    reasoningEffort?: string;
  }>;
  prompt?: DshRpcMethod<{
    sessionId: string;
    mode: "queue";
    content: readonly DshPromptContentPart[];
  }>;
}

export interface DshEventsApi {
  mux?: (
    request: DshRpcRequest<{ since?: Readonly<Record<string, number>> }>,
    signal: AbortSignal,
  ) => AsyncIterable<unknown> | Promise<AsyncIterable<unknown>>;
}

export interface DshApiProxy {
  sessions?: DshSessionsApi;
  events?: DshEventsApi;
}

/** Configuration is explicit; there is no default native model provider. */
export interface DshDriverOptions {
  nativeProvider?: string;
}

const SOURCE_HISTORY_PROJECTION = "dsh.session.projection.tokenUsage";
const SOURCE_HISTORY_EVENTS = "dsh.session-event.fold";
const SOURCE_MUX = "dsh.events.mux";
const OPEN_UNAVAILABLE_REASON = "DSH public ApiProxy has no session-specific native UI open operation";
const PERMISSIONS_UNAVAILABLE_REASON = "DSH public ApiProxy has no per-prompt permission field; the explicit permission boundary is validated at the driver boundary";

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function nonEmpty(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function integer(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : undefined;
}

function rpcFailure(operation: string, response: unknown): Error {
  const envelope = object(response);
  const result = object(envelope?.result) ?? envelope;
  const rpcError = object(result?.error);
  const code = nonEmpty(rpcError?.code);
  const detail = nonEmpty(rpcError?.message) ?? "native operation was rejected";
  return new Error(`${operation} failed${code === undefined ? "" : ` (${code})`}: ${detail}`);
}

async function invoke<P, T>(method: DshRpcMethod<P>, payload: P, operation: string): Promise<T> {
  const response = await method({ rpcId: randomUUID(), payload });
  const envelope = object(response);
  const result = object(envelope?.result) ?? envelope;
  if (result?.ok !== true) throw rpcFailure(operation, response);
  return result.value as T;
}

function unavailable(capability: string, message: string): never {
  throw new AgentDriverCapabilityError(capability, message, "dsh");
}

function capability(
  status: "available" | "unavailable",
  scope: string,
  reason?: string,
): { status: "available" | "unavailable"; scope: string; reason?: string } {
  return reason === undefined ? { status, scope } : { status, scope, reason };
}

function validateRoute(route: AgentDriverRoute | undefined, options: DshDriverOptions): AgentDriverRoute {
  if (route === undefined) unavailable("agent.route", "DSH requires an explicit native provider, model, thinking level, and permission mapping");
  const provider = route.provider === "dsh" ? options.nativeProvider : route.provider;
  if (provider === undefined || !provider.trim()) {
    unavailable("agent.route", "DSH requires an explicit native provider; no default provider is selected");
  }
  if (!route.model.trim() || !route.thinking.trim()) {
    unavailable("agent.route", "DSH requires non-empty model and thinking values before a native call");
  }
  const permissions = object(route.permissions);
  if (permissions === undefined || Object.keys(permissions).length === 0) {
    unavailable("agent.route", "DSH requires an explicit non-empty permissions mapping before a native call");
  }
  return { provider, model: route.model, thinking: route.thinking, permissions };
}

function routeForAgent(input: AgentDriverSessionInput, options: DshDriverOptions): AgentDriverRoute {
  if (input.agent.binding.provider !== "dsh") {
    unavailable("agent.route", `agent binding provider ${input.agent.binding.provider} does not match driver dsh`);
  }
  const persistedNativeProvider = input.agent.binding.nativeProvider;
  if (input.route !== undefined) {
    const resolved = validateRoute(
      input.route.provider === "dsh" && persistedNativeProvider !== undefined
        ? { ...input.route, provider: persistedNativeProvider }
        : input.route,
      options,
    );
    if (persistedNativeProvider !== undefined && resolved.provider !== persistedNativeProvider) {
      throw new Error("native provider route differs from the Agent's persisted native provider");
    }
    return resolved;
  }
  const nativeProvider = persistedNativeProvider ?? options.nativeProvider;
  if (nativeProvider === undefined) {
    unavailable("agent.route", "DSH requires an explicit native provider; configure DshDriverOptions or pass route.provider");
  }
  return validateRoute(explicitRouteForAgent(input.agent, "dsh", nativeProvider), options);
}

function routeForAttach(input: AgentDriverAttachInput, options: DshDriverOptions): AgentDriverRoute {
  return validateRoute(input.route, options);
}

interface CreateRoute {
  provider: string;
  model: string;
  thinking: string;
  permissionMode: string;
  permissions: Readonly<Record<string, unknown>>;
}

function routeForCreate(input: AgentDriverCreateInput, options: DshDriverOptions): CreateRoute {
  const provider = nonEmpty(input.nativeProvider) ?? nonEmpty(options.nativeProvider);
  const model = nonEmpty(input.model);
  const thinking = nonEmpty(input.thinking);
  const permissionMode = nonEmpty(input.permissionMode);
  if (provider === undefined || model === undefined || thinking === undefined || permissionMode === undefined) {
    unavailable("agent.create", "DSH create requires explicit native provider, model, thinking, and permission mode");
  }
  const supplied = input.permissions === undefined ? {} : object(input.permissions);
  if (supplied === undefined) unavailable("agent.create", "DSH create requires an explicit permissions mapping");
  const suppliedMode = supplied.mode;
  if (suppliedMode !== undefined && suppliedMode !== permissionMode) {
    throw new Error("permissionMode and permissions.mode must identify the same native permission boundary");
  }
  const permissions = { ...supplied, mode: permissionMode };
  return { provider, model, thinking, permissionMode, permissions };
}

function sessionIdOf(agent: TeamAgent): string {
  const sessionId = nonEmpty(agent.binding.nativeSessionId);
  if (sessionId === undefined) throw new Error("agent binding has no native session id");
  return sessionId;
}

function listItems(value: unknown): DshSessionSummary[] {
  const data = object(value);
  if (!Array.isArray(data?.items)) throw new Error("DSH sessions.list returned an invalid items array");
  return data.items.map((item, index) => {
    const row = object(item);
    const sessionId = nonEmpty(row?.sessionId);
    if (row === undefined || sessionId === undefined) throw new Error(`DSH sessions.list item ${index} has no sessionId`);
    return { ...row, sessionId } as DshSessionSummary;
  });
}

function summaryFor(items: readonly DshSessionSummary[], nativeSessionId: string): DshSessionSummary | undefined {
  return items.find(item => item.sessionId === nativeSessionId);
}

function observationIdForEvent(event: Record<string, unknown>): string | undefined {
  const seq = integer(event.seq);
  if (seq !== undefined && seq >= 0) return `seq:${seq}`;
  const id = nonEmpty(event.id);
  return id === undefined ? undefined : id;
}

function observedAtForEvent(event: Record<string, unknown>): string | undefined {
  const time = nonEmpty(event.time);
  if (time !== undefined) return time;
  const data = object(event.data);
  return nonEmpty(data?.time);
}

function count(value: Record<string, unknown>, names: readonly string[]): number | null {
  for (const name of names) {
    if (!(name in value)) continue;
    const candidate = value[name];
    if (candidate === null) return null;
    if (typeof candidate === "number" && Number.isSafeInteger(candidate) && candidate >= 0) return candidate;
    throw new Error(`native usage field ${name} must be a non-negative integer or null`);
  }
  return null;
}

function nativeUsage(
  raw: Record<string, unknown>,
  source: string,
  accounting: "delta" | "cumulative",
  observationId: string,
  observedAt?: string,
): NativeUsageObservation | undefined {
  const result: NativeUsageObservation = {
    source,
    accounting,
    inputTokens: count(raw, ["inputTokens", "uncachedInputTokens", "input_tokens"]),
    cachedInputTokens: count(raw, ["cacheReadTokens", "cachedInputTokens", "cached_input_tokens", "cache_read_tokens"]),
    outputTokens: count(raw, ["outputTokens", "output_tokens"]),
    reasoningOutputTokens: count(raw, ["reasoningOutputTokens", "reasoningTokens", "reasoning_output_tokens"]),
    totalTokens: count(raw, ["totalTokens", "total_tokens"]),
    observationId,
  };
  if (Object.values(result).every(value => value === null || value === undefined || typeof value === "string")) return undefined;
  return observedAt === undefined ? result : { ...result, observedAt };
}

function eventOf(value: unknown): Record<string, unknown> | undefined {
  const wrapper = object(value);
  const nested = object(wrapper?.event);
  return nested ?? wrapper;
}

function usagePayload(event: Record<string, unknown>): Record<string, unknown> | undefined {
  const data = object(event.data);
  if (data === undefined) return undefined;
  if (event.type === "assistant/chunk") {
    const chunk = object(data.chunk);
    if (chunk?.type !== "usage") return undefined;
    return object(chunk.usage);
  }
  if (event.type === "assistant/message") return object(data.usage);
  return undefined;
}

function usageForEvent(event: Record<string, unknown>): NativeUsageObservation | undefined {
  const raw = usagePayload(event);
  const observationId = observationIdForEvent(event);
  if (raw === undefined || observationId === undefined) return undefined;
  return nativeUsage(raw, "dsh.session-event", "delta", observationId, observedAtForEvent(event));
}

function projectionUsage(value: Record<string, unknown>): NativeUsageObservation | undefined {
  const projections = object(value.projections);
  const values = object(projections?.values);
  const tokenUsage = object(values?.tokenUsage);
  const asOfSeq = integer(projections?.asOfSeq);
  if (tokenUsage === undefined || asOfSeq === undefined || asOfSeq < 0) return undefined;
  return nativeUsage(tokenUsage, SOURCE_HISTORY_PROJECTION, "cumulative", `seq:${asOfSeq}`);
}

function sampleKey(event: Record<string, unknown>): string | undefined {
  const data = object(event.data);
  const turn = integer(data?.turn);
  const step = integer(data?.step);
  if (turn !== undefined && step !== undefined) return `turn:${turn}:step:${step}`;
  const id = observationIdForEvent(event);
  return id === undefined ? undefined : `event:${id}`;
}

function sumSamples(samples: readonly NativeUsageObservation[]): NativeUsageObservation | undefined {
  if (samples.length === 0) return undefined;
  const sum = (field: keyof Pick<NativeUsageObservation, "inputTokens" | "cachedInputTokens" | "outputTokens" | "reasoningOutputTokens" | "totalTokens">): number | null => {
    let total = 0;
    for (const sample of samples) {
      const value = sample[field];
      if (value === null) return null;
      total += value;
    }
    return total;
  };
  const latest = samples[samples.length - 1];
  return {
    source: SOURCE_HISTORY_EVENTS,
    accounting: "cumulative",
    inputTokens: sum("inputTokens"),
    cachedInputTokens: sum("cachedInputTokens"),
    outputTokens: sum("outputTokens"),
    reasoningOutputTokens: sum("reasoningOutputTokens"),
    totalTokens: sum("totalTokens"),
    observationId: latest.observationId,
    ...(latest.observedAt === undefined ? {} : { observedAt: latest.observedAt }),
  };
}

function foldedUsage(entries: readonly DshHistoryEntry[]): NativeUsageObservation | undefined {
  const samples = new Map<string, NativeUsageObservation>();
  for (const entry of entries) {
    const event = eventOf(entry);
    if (event === undefined) continue;
    const usage = usageForEvent(event);
    const key = sampleKey(event);
    if (usage !== undefined && key !== undefined) samples.set(key, usage);
  }
  return sumSamples([...samples.values()]);
}

function historyEntries(value: unknown): DshHistoryEntry[] {
  const data = object(value);
  if (!Array.isArray(data?.events)) throw new Error("DSH sessions.history returned an invalid events array");
  return data.events.map((entry, index) => {
    if (object(entry) === undefined) throw new Error(`DSH history entry ${index} is not an object`);
    return entry as DshHistoryEntry;
  });
}

function hasMore(value: unknown): boolean {
  return object(value)?.hasMore === true;
}

function oldestSeq(entries: readonly DshHistoryEntry[]): number | undefined {
  const values = entries.map(entry => integer(eventOf(entry)?.seq)).filter((seq): seq is number => seq !== undefined);
  return values.length === 0 ? undefined : Math.min(...values);
}

function lifecycleStatus(event: Record<string, unknown>): string | undefined {
  switch (event.type) {
    case "turn/start": return "running";
    case "turn/end": return "idle";
    case "agent/error":
    case "session/error": return "failed";
    default: return undefined;
  }
}

function muxFrame(value: unknown): Record<string, unknown> | undefined {
  const wrapper = object(value);
  return object(wrapper?.payload) ?? wrapper;
}

function discoveryFor(
  apiProxy: DshApiProxy,
  options: DshDriverOptions,
): AgentDriverDiscovery {
  const sessions = apiProxy.sessions;
  const hasList = typeof sessions?.list === "function";
  const hasCreate = typeof sessions?.create === "function";
  const hasHistory = typeof sessions?.history === "function";
  const hasSelectModel = typeof sessions?.selectModel === "function";
  const hasPrompt = typeof sessions?.prompt === "function";
  const hasMux = typeof apiProxy.events?.mux === "function";
  const sendReason = !hasPrompt
    ? "DSH public sessions.prompt is unavailable"
    : !hasSelectModel
      ? "DSH public sessions.selectModel is required to apply the explicit model/thinking route"
      : undefined;
  const capabilities: AgentDriverCapabilities = {
    discover: capability("available", "offline-api-surface"),
    attach: capability(hasList ? "available" : "unavailable", "native-session", hasList ? undefined : "DSH public sessions.list is unavailable"),
    create: capability(hasCreate && hasSelectModel ? "available" : "unavailable", "native-session", hasCreate && hasSelectModel ? undefined : "DSH create requires sessions.create and sessions.selectModel"),
    send: capability(hasPrompt && hasSelectModel ? "available" : "unavailable", "native-session", sendReason),
    resume: capability(hasPrompt && hasSelectModel ? "available" : "unavailable", "cold-resume", sendReason),
    status: capability(hasList ? "available" : "unavailable", "sessions.list", hasList ? undefined : "DSH public sessions.list is unavailable"),
    watch: capability(hasMux ? "available" : "unavailable", "events.mux", hasMux ? undefined : "DSH public events.mux is unavailable"),
    open: capability("unavailable", "native-ui", OPEN_UNAVAILABLE_REASON),
    usage: capability(hasHistory ? "available" : "unavailable", "sessions.history", hasHistory ? undefined : "DSH public sessions.history is unavailable"),
  };
  return {
    provider: "dsh",
    available: true,
    status: "available",
    capabilities,
    configuration: {
      routeRequired: true,
      nativeProvider: options.nativeProvider ?? "caller-supplied",
      model: "caller-supplied",
      thinking: "caller-supplied",
      permissions: "caller-supplied",
      quotaProbe: false,
      permissionForwarding: PERMISSIONS_UNAVAILABLE_REASON,
    },
  };
}

export function createDshDriver(apiProxy: DshApiProxy | undefined, options: DshDriverOptions = {}): AgentDriver | undefined {
  if (apiProxy === undefined) return undefined;
  const discovery = discoveryFor(apiProxy, options);
  const sessions = apiProxy.sessions;

  const driver: AgentDriver = {
    provider: "dsh",
    capabilities: discovery.capabilities,
    requiresExplicitRoute: true,
    discover: () => discovery,

    async attach(input) {
      routeForAttach(input, options);
      const list = sessions?.list;
      if (typeof list !== "function") unavailable("agent.attach", "DSH public sessions.list is unavailable");
      const items = listItems(await invoke(list, {}, "sessions.list"));
      const found = summaryFor(items, input.nativeSessionId);
      if (found === undefined) throw new Error(`DSH native session ${input.nativeSessionId} was not found`);
      return {
        nativeSessionId: found.sessionId,
        nativeOpenRef: input.nativeOpenRef ?? null,
        evidence: {
          source: "dsh.sessions.list",
          running: found.running ?? null,
          blank: found.blank ?? null,
          updatedAt: found.updatedAt ?? null,
        },
      };
    },

    async create(input) {
      const route = routeForCreate(input, options);
      const create = sessions?.create;
      const selectModel = sessions?.selectModel;
      if (typeof create !== "function") unavailable("agent.create", "DSH public sessions.create is unavailable");
      if (typeof selectModel !== "function") unavailable("agent.create", "DSH public sessions.selectModel is required to apply the explicit route");
      const cwd = nonEmpty(route.permissions.cwd) ?? nonEmpty(route.permissions.workspace);
      const createdValue = await invoke(create, cwd === undefined ? {} : { cwd }, "sessions.create");
      const created = object(createdValue);
      const nativeSessionId = nonEmpty(created?.sessionId);
      if (nativeSessionId === undefined) throw new Error("DSH sessions.create returned no native session id");
      await invoke(selectModel, {
        sessionId: nativeSessionId,
        provider: route.provider,
        model: route.model,
        reasoningEffort: route.thinking,
      }, "sessions.selectModel");
      return {
        nativeSessionId,
        nativeOpenRef: null,
        evidence: {
          source: "dsh.sessions.create+selectModel",
          nativeProvider: route.provider,
          model: route.model,
          thinking: route.thinking,
          permissionMode: route.permissionMode,
          permissionsForwarded: false,
          permissionsBoundary: route.permissions,
          workspaceId: input.workspaceId,
        },
      };
    },

    async send(input) {
      const route = routeForAgent(input, options);
      const selectModel = sessions?.selectModel;
      const prompt = sessions?.prompt;
      if (typeof selectModel !== "function") unavailable("agent.send", "DSH public sessions.selectModel is required to apply the explicit model/thinking route");
      if (typeof prompt !== "function") unavailable("agent.send", "DSH public sessions.prompt is unavailable");
      const sessionId = sessionIdOf(input.agent);
      await invoke(selectModel, {
        sessionId,
        provider: route.provider,
        model: route.model,
        reasoningEffort: route.thinking,
      }, "sessions.selectModel");
      if (typeof input.content !== "string" || !input.content.trim()) throw new Error("driver content must be non-empty text");
      const result = await invoke<{
        sessionId: string;
        mode: "queue";
        content: readonly DshPromptContentPart[];
      }, { accepted?: boolean }>(prompt, {
        sessionId,
        mode: "queue",
        content: [{ type: "text", text: input.content }],
      }, "sessions.prompt");
      if (result?.accepted === false) throw new Error("DSH session rejected the prompt");
      // DSH emits usage through history/mux events; prompt admission itself
      // carries no token counters, so do not synthesize a usage observation.
      return { accepted: true };
    },

    async resume(input) {
      return driver.send(input);
    },

    async status(input) {
      routeForAgent(input, options);
      const list = sessions?.list;
      if (typeof list !== "function") unavailable("agent.status", "DSH public sessions.list is unavailable");
      const nativeSessionId = sessionIdOf(input.agent);
      const found = summaryFor(listItems(await invoke(list, {}, "sessions.list")), nativeSessionId);
      if (found === undefined) {
        return {
          nativeSessionId,
          taskExists: false,
          hostStatus: "not-found",
          resultAvailable: null,
          needsAttention: null,
          sourceRef: "dsh.sessions.list",
          reason: "DSH sessions.list did not expose this native session",
        };
      }
      return {
        nativeSessionId,
        taskExists: true,
        hostStatus: found.running === true ? "running" : "idle",
        resultAvailable: null,
        needsAttention: null,
        sourceRef: "dsh.sessions.list",
        reason: "DSH sessions.list exposes running only; result/attention state is unavailable",
      };
    },

    async *watch(input) {
      const route = routeForAgent(input, options);
      void route;
      const mux = apiProxy.events?.mux;
      if (typeof mux !== "function") unavailable("agent.watch", "DSH public events.mux is unavailable");
      const nativeSessionId = sessionIdOf(input.agent);
      const signal = input.signal ?? new AbortController().signal;
      const stream = await mux.call(apiProxy.events, {
        rpcId: randomUUID(),
        payload: { since: { [nativeSessionId]: 0 } },
      }, signal);
      for await (const raw of stream) {
        if (signal.aborted) return;
        const frame = muxFrame(raw);
        if (frame === undefined || frame.sessionId !== nativeSessionId) continue;
        if (frame.type === "session/subscribed") {
          yield {
            nativeSessionId,
            type: "status",
            status: "subscribed",
            event: frame,
            sourceRef: SOURCE_MUX,
          };
          continue;
        }
        if (frame.type !== "session/event") {
          yield { nativeSessionId, type: "event", event: frame, sourceRef: SOURCE_MUX };
          continue;
        }
        const event = eventOf(frame.event);
        const usage = event === undefined ? undefined : usageForEvent(event);
        yield {
          nativeSessionId,
          type: "event",
          ...(event === undefined ? {} : { status: lifecycleStatus(event), event }),
          ...(usage === undefined ? {} : { usage }),
          sourceRef: SOURCE_MUX,
        };
      }
    },

    async open(input): Promise<AgentDriverOpenResult> {
      const nativeSessionId = sessionIdOf(input.agent);
      return {
        status: "unavailable",
        nativeSessionId,
        nativeOpenRef: null,
        reason: OPEN_UNAVAILABLE_REASON,
      };
    },

    async usage(input): Promise<AgentDriverUsageResult> {
      routeForAgent(input, options);
      const history = sessions?.history;
      if (typeof history !== "function") unavailable("agent.usage", "DSH public sessions.history is unavailable");
      const nativeSessionId = sessionIdOf(input.agent);
      const entries: DshHistoryEntry[] = [];
      let beforeSeq: number | undefined;
      let firstPage = true;
      for (let page = 0; page < 100; page += 1) {
        const value = await invoke(history, {
          sessionId: nativeSessionId,
          ...(beforeSeq === undefined ? {} : { beforeSeq }),
          maxMessages: 100,
        }, "sessions.history");
        const pageEntries = historyEntries(value);
        if (firstPage) {
          const projected = projectionUsage(object(value) ?? {});
          if (projected !== undefined) return { status: "available", nativeSessionId, usage: projected };
          firstPage = false;
        }
        entries.unshift(...pageEntries);
        if (!hasMore(value)) break;
        const nextBefore = oldestSeq(pageEntries);
        if (nextBefore === undefined || nextBefore === beforeSeq) break;
        beforeSeq = nextBefore;
      }
      const usage = foldedUsage(entries);
      return usage === undefined
        ? { status: "unavailable", nativeSessionId, reason: "DSH history contained no native token usage observation" }
        : { status: "available", nativeSessionId, usage };
    },
  };
  return driver;
}
