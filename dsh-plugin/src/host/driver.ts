import type { AgentDriverResult, TeamAgent, UsageAccounting } from "../shared/types.js";

/**
 * Provider-neutral capability names.  The host owns collaboration state; a
 * driver owns only the native transport and may honestly leave any capability
 * unavailable when its provider does not expose it.
 */
export const AGENT_DRIVER_CAPABILITIES = [
  "discover",
  "attach",
  "create",
  "send",
  "resume",
  "status",
  "watch",
  "open",
  "usage",
] as const;

export type AgentDriverCapabilityName = (typeof AGENT_DRIVER_CAPABILITIES)[number];
export type AgentDriverCapabilityStatus = "available" | "unavailable";

export interface AgentDriverCapability {
  status: AgentDriverCapabilityStatus;
  /** Provider-owned boundary, for example "native-session" or "during-send". */
  scope?: string;
  reason?: string;
}

export type AgentDriverCapabilities = Readonly<Record<AgentDriverCapabilityName, AgentDriverCapability>>;

export interface AgentDriverDiscovery {
  provider: string;
  available: boolean;
  status: AgentDriverCapabilityStatus;
  capabilities: AgentDriverCapabilities;
  /** Provider-owned route facts; this is descriptive, never a quota probe. */
  configuration?: Readonly<Record<string, unknown>>;
  reason?: string;
}

/**
 * Exact route approved by the caller.  A driver must not invent defaults for
 * these fields before making a native call.
 */
export interface AgentDriverRoute {
  /** Provider id owned by the native driver, not the transport driver id. */
  provider: string;
  model: string;
  thinking: string;
  permissions: Readonly<Record<string, unknown>>;
}

export interface DriverInput {
  workspaceId: string;
  agent: TeamAgent;
  content: string;
  packetVersions?: number[];
  /** Populated by the host when the Agent has an explicit route. */
  route?: AgentDriverRoute;
}

export interface AgentDriverAttachInput {
  workspaceId?: string;
  displayName?: string;
  nativeSessionId: string;
  nativeOpenRef?: string | null;
  /** An attach-capable driver may require this before verifying the native id. */
  route?: AgentDriverRoute;
}

export interface AgentDriverAttachment {
  nativeSessionId: string;
  nativeOpenRef?: string | null;
  /** Provider-native evidence, when the provider returned it. */
  evidence?: Readonly<Record<string, unknown>>;
}

export interface AgentDriverCreateInput {
  workspaceId: string;
  displayName: string;
  model?: string;
  thinking?: string;
  permissionMode?: string;
  permissions?: Readonly<Record<string, unknown>>;
  responsibility?: string;
  /** A provider-specific model route, when it is distinct from the driver id. */
  nativeProvider?: string;
}

export interface AgentDriverSessionInput {
  agent: TeamAgent;
  route?: AgentDriverRoute;
  signal?: AbortSignal;
}

export interface AgentDriverObservation {
  nativeSessionId: string;
  taskExists: boolean;
  /** Provider status is intentionally opaque; no terminal meaning is inferred. */
  hostStatus: string;
  /** Null means the provider did not expose this native fact. */
  resultAvailable: boolean | null;
  needsAttention: boolean | null;
  sourceRef: string;
  usage?: NativeUsageObservation;
  reason?: string;
}

export interface NativeUsageObservation {
  source: string;
  accounting: UsageAccounting;
  inputTokens: number | null;
  cachedInputTokens: number | null;
  outputTokens: number | null;
  reasoningOutputTokens: number | null;
  totalTokens: number | null;
  /** Stable native observation identity, never rewritten as a global id. */
  observationId: string;
  observedAt?: string;
}

export interface AgentDriverWatchEvent {
  nativeSessionId: string;
  type: "status" | "event";
  status?: string;
  event?: unknown;
  usage?: NativeUsageObservation;
  sourceRef: string;
}

export interface AgentDriverOpenResult {
  status: AgentDriverCapabilityStatus;
  nativeSessionId: string;
  nativeOpenRef?: string | null;
  reason?: string;
}

export interface AgentDriverUsageResult {
  status: AgentDriverCapabilityStatus;
  nativeSessionId: string;
  usage?: NativeUsageObservation;
  reason?: string;
}

/** A provider can use this error without importing the host implementation. */
export class AgentDriverCapabilityError extends Error {
  readonly code = "capability-unavailable" as const;

  constructor(
    readonly capability: string,
    message: string,
    readonly provider?: string,
  ) {
    super(message);
    this.name = "AgentDriverCapabilityError";
  }
}

export interface AgentDriver {
  provider: string;
  /** Static, truthful capability advertisement; discovery itself is offline. */
  capabilities?: AgentDriverCapabilities;
  discover?: () => AgentDriverDiscovery;
  attach?: (input: AgentDriverAttachInput) => Promise<AgentDriverAttachment>;
  send(input: DriverInput): Promise<AgentDriverResult>;
  /** A distinct alias is useful when a provider exposes resume semantics. */
  resume?: (input: DriverInput) => Promise<AgentDriverResult>;
  create?: (input: AgentDriverCreateInput) => Promise<AgentDriverAttachment>;
  status?: (input: AgentDriverSessionInput) => Promise<AgentDriverObservation>;
  watch?: (input: AgentDriverSessionInput) => AsyncIterable<AgentDriverWatchEvent>;
  open?: (input: AgentDriverSessionInput) => Promise<AgentDriverOpenResult>;
  usage?: (input: AgentDriverSessionInput) => Promise<AgentDriverUsageResult>;
  /**
   * When true, the driver requires an explicit model/thinking/permission
   * route before send/delivery. The host keeps this metadata for discovery;
   * the driver remains responsible for validating its own native boundary.
   */
  requiresExplicitRoute?: boolean;
}

export function explicitRouteForAgent(
  agent: TeamAgent,
  driverProvider = agent.binding.provider,
  nativeProvider = driverProvider,
): AgentDriverRoute {
  if (agent.binding.provider !== driverProvider) {
    throw new AgentDriverCapabilityError(
      "agent.route",
      `agent binding provider ${agent.binding.provider} does not match driver ${driverProvider}`,
      driverProvider,
    );
  }
  if (!agent.model || !agent.thinking || !agent.permissionMode) {
    throw new AgentDriverCapabilityError(
      "agent.route",
      `driver ${driverProvider} requires explicit model, thinking, and permission mode before a native call`,
      driverProvider,
    );
  }
  if (typeof nativeProvider !== "string" || !nativeProvider.trim()) {
    throw new AgentDriverCapabilityError(
      "agent.route",
      `driver ${driverProvider} requires an explicit native provider before a native call`,
      driverProvider,
    );
  }
  return {
    provider: nativeProvider,
    model: agent.model,
    thinking: agent.thinking,
    permissions: { mode: agent.permissionMode },
  };
}

export function nativeUsageToDriverResult(usage: NativeUsageObservation): NonNullable<AgentDriverResult["usage"]> {
  return {
    source: usage.source,
    accounting: usage.accounting,
    inputTokens: usage.inputTokens,
    cachedInputTokens: usage.cachedInputTokens,
    outputTokens: usage.outputTokens,
    reasoningOutputTokens: usage.reasoningOutputTokens,
    totalTokens: usage.totalTokens,
    observationId: usage.observationId,
    ...(usage.observedAt === undefined ? {} : { observedAt: usage.observedAt }),
  };
}

/** A structural guard used at the host boundary for provider errors. */
export function isAgentDriverCapabilityError(error: unknown): error is AgentDriverCapabilityError {
  return error instanceof AgentDriverCapabilityError
    || (typeof error === "object" && error !== null
      && (error as { code?: unknown }).code === "capability-unavailable"
      && typeof (error as { capability?: unknown }).capability === "string");
}
