/**
 * Transport-neutral Visible Team contract.
 *
 * The core deliberately does not know DSH workspaces, sessions, or providers.
 * A host/client adapter may put an opaque binding in `hostBinding` and an
 * opaque provider/session identity in `AgentBinding`.
 */

export const API_PATH = "/api/visible-team/workspaces";
export const CONTEXT_PATH = "/api/visible-team/context";
export const EVENTS_PATH = "/api/visible-team/events";

export type WorkspaceStatus = "active" | "paused" | "completed" | "cancelled";
export type AgentStatus = "planned" | "active" | "idle" | "waiting" | "blocked" | "completed" | "failed" | "cancelled";
export type AgentAttachSource = "created" | "manual" | "discovered";
export type UsageAccounting = "delta" | "cumulative";

/** Opaque binding owned by an adapter; the core only persists it. */
export interface HostBinding {
  kind: string;
  ref: string;
}

/** The only identity the collaboration core needs for an attached Agent. */
export interface AgentBinding {
  provider: string;
  nativeSessionId: string;
  nativeOpenRef: string | null;
}

export interface UsageObservation {
  /** Native IDs are scoped by Agent and source; drivers must not rewrite them for global uniqueness. */
  observationId: string;
  source: string;
  accounting: UsageAccounting;
  inputTokens: number | null;
  cachedInputTokens: number | null;
  outputTokens: number | null;
  reasoningOutputTokens: number | null;
  totalTokens: number | null;
  observedAt: string;
}

export interface AgentUsageSummary {
  inputTokens: number | null;
  cachedInputTokens: number | null;
  outputTokens: number | null;
  reasoningOutputTokens: number | null;
  totalTokens: number | null;
  /** Null means no native usage observation was received. */
  accounting: UsageAccounting | null;
  latestObservationId: string | null;
}

export interface TeamAgent {
  agentId: string;
  workspaceId: string;
  displayName: string;
  binding: AgentBinding;
  model: string | null;
  thinking: string | null;
  permissionMode: string | null;
  responsibility: string;
  status: AgentStatus;
  attachSource: AgentAttachSource;
  contextVersion: number;
  pendingContext: number;
  usage: AgentUsageSummary;
  createdAt: string;
  updatedAt: string;
}

export interface ContextPacket {
  updateId: number;
  workspaceId: string;
  version: number;
  summary: string;
  sourceRef: string | null;
  createdBy: string;
  targets: string[];
  deliveredAt: string | null;
  createdAt: string;
}

export interface TeamWorkspace {
  workspaceId: string;
  title: string;
  objective: string;
  sharedRules: string;
  hostBinding: HostBinding | null;
  leaderAgentId: string | null;
  status: WorkspaceStatus;
  version: number;
  agents: TeamAgent[];
  context: ContextPacket[];
  createdAt: string;
  updatedAt: string;
}

export type WorkspaceAction =
  | {
      action: "create-workspace";
      title: string;
      objective: string;
      sharedRules?: string;
      hostBinding?: HostBinding;
    }
  | {
      action: "update-workspace";
      workspaceId: string;
      title?: string;
      objective?: string;
      sharedRules?: string;
      hostBinding?: HostBinding | null;
      leaderAgentId?: string | null;
      status?: WorkspaceStatus;
    }
  | {
      /** Attach an already existing native session/task. */
      action: "attach-agent";
      workspaceId: string;
      displayName: string;
      binding: {
        provider: string;
        nativeSessionId: string;
        nativeOpenRef?: string;
      };
      model?: string;
      thinking?: string;
      permissionMode?: string;
      responsibility?: string;
      attachSource?: Exclude<AgentAttachSource, "created">;
      asLeader?: boolean;
    }
  | {
      /** Creation is driver-owned; no driver means capability-unavailable. */
      action: "create-agent";
      workspaceId: string;
      displayName: string;
      provider: string;
      model?: string;
      thinking?: string;
      permissionMode?: string;
      responsibility?: string;
      asLeader?: boolean;
    }
  | {
      action: "add-context";
      workspaceId: string;
      summary: string;
      sourceRef?: string;
      createdBy?: string;
      /** Explicit target list; the value `all` is never accepted. */
      targets: string[];
    }
  | {
      /** Send only this Agent's pending packets, then acknowledge them. */
      action: "deliver-context";
      workspaceId: string;
      agentId: string;
      throughVersion?: number;
    }
  | {
      /** Direct user/Leader instruction through the Agent's selected driver. */
      action: "send-agent";
      workspaceId: string;
      agentId: string;
      content: string;
    }
  | { action: "ack-context"; workspaceId: string; agentId: string; throughVersion: number }
  | {
      action: "record-usage";
      workspaceId: string;
      agentId: string;
      observationId: string;
      source: string;
      accounting: UsageAccounting;
      observedAt?: string;
      inputTokens?: number;
      cachedInputTokens?: number;
      outputTokens?: number;
      reasoningOutputTokens?: number;
      totalTokens?: number;
    };

export type WorkspaceStateAction = Exclude<WorkspaceAction, { action: "create-agent" | "deliver-context" | "send-agent" }>;

export interface DeliveryResult {
  action: "deliver-context" | "send-agent";
  agentId: string;
  accepted: boolean;
  deliveredVersions?: number[];
  reason?: string;
}

export interface WorkspaceCommandResult {
  workspaces: TeamWorkspace[];
  delivery?: DeliveryResult;
}

export interface ContextResponse {
  workspaceId: string;
  agentId: string;
  context: ContextPacket[];
}

export interface CapabilityUnavailable {
  code: "capability-unavailable";
  capability: string;
  provider?: string;
  message: string;
}

export interface AgentDriverResult {
  accepted: true;
  usage?: Omit<UsageObservation, "observationId" | "observedAt"> & {
    observationId?: string;
    observedAt?: string;
  };
}

export interface WorkspaceResponse {
  workspaces: TeamWorkspace[];
}
