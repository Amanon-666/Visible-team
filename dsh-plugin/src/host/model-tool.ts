import { defineTool } from "@deepseek-ai/dsh-tools";
import type { ContextPacket, TeamAgent, TeamWorkspace, WorkspaceAction, WorkspaceCommandResult } from "../shared/types.js";
import { VisibleTeamStore } from "./store.js";

/** The one model-facing capability exposed by the plugin. */
export const VISIBLE_TEAM_TOOL_NAME = "visible_team";

/** DSH is the only provider whose Agent identity is accepted by this tool. */
export const DSH_PROVIDER = "dsh";

const OPERATIONS = [
  "list_workspaces",
  "read_workspace",
  "read_pending_context",
  "send_message",
  "deliver_context",
  "progress",
] as const;

type VisibleTeamOperation = (typeof OPERATIONS)[number];
type WorkspaceReadOperation = Extract<VisibleTeamOperation, "list_workspaces" | "read_workspace">;

const MAX_WORKSPACE_TEXT = 400;
const MAX_AGENT_NAME = 100;
const MAX_CONTEXT_PACKETS = 8;
const MAX_CONTEXT_SUMMARY = 1_000;
const MAX_SOURCE_REF = 160;
const MAX_MESSAGE = 8_000;
const MAX_PROGRESS = 2_000;
const MAX_AGENTS = 24;

/** A small seam that keeps the host independent from a concrete Cordis Context type. */
export interface ToolRuntimeLike {
  register(definition: ReturnType<typeof defineTool>): () => void;
}

export type ModelActionExecutor = (action: WorkspaceAction) => Promise<WorkspaceCommandResult>;

export interface CallingTeamAgent {
  agent: TeamAgent;
  workspace: TeamWorkspace;
  isLeader: boolean;
}

interface VisibleTeamToolArgs {
  operation: VisibleTeamOperation;
  workspaceId?: string;
  agentId?: string;
  content?: string;
  throughVersion?: number;
  summary?: string;
  targetAgentId?: string;
}

interface WorkspaceAgentSummary {
  agentId: string;
  displayName: string;
  status: TeamAgent["status"];
  pendingContext: number;
  contextVersion: number;
  isLeader: boolean;
}

interface PendingContextSummary {
  updateId: number;
  version: number;
  summary: string;
  sourceRef: string | null;
}

interface WorkspaceListResult {
  kind: "workspace-list";
  operation: "list_workspaces";
  workspaceId: string;
  title: string;
  objective: string;
  status: TeamWorkspace["status"];
  version: number;
  leaderAgentId: string | null;
  agentCount: number;
  agents: WorkspaceAgentSummary[];
  truncated: boolean;
}

interface WorkspaceReadResult extends Omit<WorkspaceListResult, "kind" | "operation"> {
  kind: "workspace";
  operation: "read_workspace";
}

interface PendingContextResult {
  kind: "pending-context";
  operation: "read_pending_context";
  workspaceId: string;
  agentId: string;
  pendingCount: number;
  pending: PendingContextSummary[];
  truncated: boolean;
}

interface ActionResult {
  kind: "action";
  operation: Exclude<VisibleTeamOperation, WorkspaceReadOperation | "read_pending_context">;
  workspaceId: string;
  agentId?: string;
  targetAgentId?: string;
  accepted?: boolean;
  deliveredVersions?: number[];
  version?: number;
  pendingContext?: number;
  reason?: string;
}

export type VisibleTeamToolResult =
  | WorkspaceListResult
  | WorkspaceReadResult
  | PendingContextResult
  | ActionResult;

const agentSummarySchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    agentId: { type: "string", required: true },
    displayName: { type: "string", required: true },
    status: { type: "string", required: true, enum: ["planned", "active", "idle", "waiting", "blocked", "completed", "failed", "cancelled"] },
    pendingContext: { type: "integer", required: true },
    contextVersion: { type: "integer", required: true },
    isLeader: { type: "boolean", required: true },
  },
} as const;

const pendingContextSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    updateId: { type: "integer", required: true },
    version: { type: "integer", required: true },
    summary: { type: "string", required: true },
    sourceRef: { oneOf: [{ type: "string" }, { type: "null" }], required: true },
  },
} as const;

const parameters = {
  operation: {
    type: "string",
    enum: OPERATIONS,
    required: true,
    description: "One of list_workspaces, read_workspace, read_pending_context, send_message, deliver_context, or progress.",
  },
  workspaceId: {
    type: "string",
    description: "Optional consistency check. It must equal the workspace bound to the calling DSH Agent; it is never an access grant.",
  },
  agentId: {
    type: "string",
    description: "Existing same-workspace Agent for read_pending_context, send_message, or deliver_context. Members may use only their own Agent.",
  },
  content: {
    type: "string",
    description: `Message for send_message (at most ${MAX_MESSAGE} characters). It is sent only by the workspace Leader to an existing Agent.`,
  },
  throughVersion: {
    type: "integer",
    description: "Optional non-negative version ceiling for read_pending_context or deliver_context.",
  },
  summary: {
    type: "string",
    description: `Short progress text for progress (at most ${MAX_PROGRESS} characters). It is stored as one targeted Context Packet.`,
  },
  targetAgentId: {
    type: "string",
    description: "Optional progress recipient. Members can target only the workspace Leader; Leaders can target an existing same-workspace Agent.",
  },
} as const;

const outputSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    kind: { type: "string", enum: ["workspace-list", "workspace", "pending-context", "action"], required: true },
    operation: { type: "string", enum: OPERATIONS, required: true },
    workspaceId: { type: "string", required: true },
    title: { type: "string" },
    objective: { type: "string" },
    status: { type: "string", enum: ["active", "paused", "completed", "cancelled"] },
    version: { type: "integer" },
    leaderAgentId: { oneOf: [{ type: "string" }, { type: "null" }] },
    agentCount: { type: "integer" },
    agents: { type: "array", items: agentSummarySchema },
    agentId: { type: "string" },
    targetAgentId: { type: "string" },
    pendingCount: { type: "integer" },
    pending: { type: "array", items: pendingContextSchema },
    truncated: { type: "boolean" },
    accepted: { type: "boolean" },
    deliveredVersions: { type: "array", items: { type: "integer" } },
    pendingContext: { type: "integer" },
    reason: { type: "string" },
  },
} as const;

function shorten(value: string, limit: number): string {
  const text = value.trim();
  return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 1))}…`;
}

function requiredText(value: unknown, field: string, limit: number): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${field} is required`);
  const text = value.trim();
  if (text.length > limit) throw new Error(`${field} is too long (maximum ${limit} characters)`);
  return text;
}

function optionalText(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim()) throw new Error(`${field} must be a non-empty string when provided`);
  return value.trim();
}

function optionalVersion(value: unknown): number | undefined {
  if (value === undefined) return undefined;
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error("throughVersion must be a non-negative integer");
  return Number(value);
}

/**
 * Resolve the caller only from the public DSH execution identity. There is no
 * workspace-id fallback: an unbound or non-DSH Agent is denied before any
 * caller-supplied workspace or target is read.
 */
export function resolveCallingTeamAgent(
  store: VisibleTeamStore,
  exec: { agent?: { id?: unknown } },
): CallingTeamAgent {
  const nativeSessionId = exec.agent?.id;
  if (typeof nativeSessionId !== "string" || !nativeSessionId.trim()) {
    throw new Error("visible_team requires a calling DSH Agent with a stable session id");
  }
  const agent = store.findAgentByNativeSession(DSH_PROVIDER, nativeSessionId.trim());
  if (agent === undefined) {
    throw new Error("calling DSH Agent is not attached to a Visible Team workspace");
  }
  const workspace = store.snapshot(agent.workspaceId);
  return {
    agent,
    workspace,
    isLeader: workspace.leaderAgentId === agent.agentId,
  };
}

function assertBoundWorkspace(args: VisibleTeamToolArgs, caller: CallingTeamAgent): void {
  const requested = optionalText(args.workspaceId, "workspaceId");
  if (requested !== undefined && requested !== caller.workspace.workspaceId) {
    throw new Error("workspaceId must match the workspace bound to the calling DSH Agent");
  }
}

function requireLeader(caller: CallingTeamAgent): void {
  if (!caller.isLeader) throw new Error("only the workspace Leader may perform this Visible Team operation");
}

function requireTargetAgent(store: VisibleTeamStore, workspace: TeamWorkspace, value: unknown, field: string): TeamAgent {
  const agentId = requiredText(value, field, 100);
  return store.getAgent(workspace.workspaceId, agentId);
}

function projectWorkspace(workspace: TeamWorkspace, operation: WorkspaceReadOperation): WorkspaceListResult | WorkspaceReadResult {
  const agents = workspace.agents.slice(0, MAX_AGENTS).map<WorkspaceAgentSummary>(agent => ({
    agentId: agent.agentId,
    displayName: shorten(agent.displayName, MAX_AGENT_NAME),
    status: agent.status,
    pendingContext: agent.pendingContext,
    contextVersion: agent.contextVersion,
    isLeader: workspace.leaderAgentId === agent.agentId,
  }));
  const title = shorten(workspace.title, MAX_WORKSPACE_TEXT);
  const objective = shorten(workspace.objective, MAX_WORKSPACE_TEXT);
  const common = {
    workspaceId: workspace.workspaceId,
    title,
    objective,
    status: workspace.status,
    version: workspace.version,
    leaderAgentId: workspace.leaderAgentId,
    agentCount: workspace.agents.length,
    agents,
    truncated: workspace.agents.length > agents.length
      || title !== workspace.title.trim()
      || objective !== workspace.objective.trim()
      || agents.some((agent, index) => agent.displayName !== workspace.agents[index]?.displayName.trim()),
  };
  return operation === "list_workspaces"
    ? { kind: "workspace-list", operation: "list_workspaces", ...common }
    : { kind: "workspace", operation: "read_workspace", ...common };
}

function projectPendingContext(
  workspaceId: string,
  agentId: string,
  packets: readonly ContextPacket[],
): PendingContextResult {
  const selected = packets.slice(0, MAX_CONTEXT_PACKETS);
  const pending = selected.map<PendingContextSummary>(packet => ({
    updateId: packet.updateId,
    version: packet.version,
    summary: shorten(packet.summary, MAX_CONTEXT_SUMMARY),
    sourceRef: packet.sourceRef === null ? null : shorten(packet.sourceRef, MAX_SOURCE_REF),
  }));
  return {
    kind: "pending-context",
    operation: "read_pending_context",
    workspaceId,
    agentId,
    pendingCount: packets.length,
    pending,
    truncated: packets.length > pending.length || selected.some((packet, index) =>
      packet.summary !== pending[index]?.summary || packet.sourceRef !== pending[index]?.sourceRef,
    ),
  };
}

function actionResult(
  operation: ActionResult["operation"],
  workspaceId: string,
  result: WorkspaceCommandResult,
  extra: Omit<ActionResult, "kind" | "operation" | "workspaceId"> = {},
): ActionResult {
  const projected: ActionResult = {
    kind: "action",
    operation,
    workspaceId,
  };
  const version = result.workspaces[0]?.version;
  if (version !== undefined) projected.version = version;
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined) Object.assign(projected, { [key]: value });
  }
  return projected;
}

function renderResult(value: VisibleTeamToolResult): { type: "text"; text: string }[] {
  switch (value.kind) {
    case "workspace-list":
      return [{ type: "text", text: `Visible Team: workspace ${value.title} (${value.workspaceId}) v${value.version}, ${value.agentCount} Agent(s).` }];
    case "workspace":
      return [{ type: "text", text: `Visible Team workspace ${value.title} v${value.version} · ${value.status} · ${value.agentCount} Agent(s).` }];
    case "pending-context": {
      const entries = value.pending.map(packet => `v${packet.version}: ${packet.summary}`);
      const body = entries.length === 0 ? "none" : entries.join("\n");
      return [{ type: "text", text: `Pending context for Agent ${value.agentId} (${value.pendingCount} packet(s)):\n${body}` }];
    }
    case "action": {
      const target = value.targetAgentId ?? value.agentId;
      const targetText = target === undefined ? "" : ` for Agent ${target}`;
      if (value.accepted === false) return [{ type: "text", text: `Visible Team ${value.operation} was not accepted${targetText}: ${value.reason ?? "no pending context"}.` }];
      if (value.operation === "progress") return [{ type: "text", text: `Visible Team progress recorded for Agent ${value.targetAgentId ?? "the Leader"} at workspace v${value.version}.` }];
      const versions = value.deliveredVersions === undefined || value.deliveredVersions.length === 0
        ? ""
        : ` v${value.deliveredVersions.join(", v")}`;
      return [{ type: "text", text: `Visible Team ${value.operation} accepted${targetText}.${versions}` }];
    }
  }
}

function operationKind(operation: VisibleTeamOperation): "read" | "other" {
  return operation === "list_workspaces" || operation === "read_workspace" || operation === "read_pending_context" ? "read" : "other";
}

/** Build the official DSH ToolDefinition over the existing Store/Action seams. */
export function createVisibleTeamModelTool(
  store: VisibleTeamStore,
  executeAction: ModelActionExecutor,
  onChange?: (workspaceId: string) => void,
): ReturnType<typeof defineTool> {
  return defineTool({
    name: VISIBLE_TEAM_TOOL_NAME,
    description: "Control only the Visible Team workspace bound to the current DSH Agent. List/read its short snapshot, read target-scoped pending context, send or deliver to existing Agents only as that workspace's Leader, or submit a short targeted progress update. workspaceId never grants access. No Agent creation, permission changes, or broadcast.",
    parameters,
    output: {
      schema: outputSchema,
      render: (_args, value) => renderResult(value as VisibleTeamToolResult),
    },
    execute: async (rawArgs, exec) => {
      const args = rawArgs as VisibleTeamToolArgs;
      const caller = resolveCallingTeamAgent(store, exec);
      assertBoundWorkspace(args, caller);
      const workspace = caller.workspace;

      switch (args.operation) {
        case "list_workspaces":
          requireLeader(caller);
          return projectWorkspace(workspace, "list_workspaces");
        case "read_workspace":
          requireLeader(caller);
          return projectWorkspace(workspace, "read_workspace");
        case "read_pending_context": {
          const requestedAgentId = optionalText(args.agentId, "agentId");
          const agentId = requestedAgentId ?? caller.agent.agentId;
          if (!caller.isLeader && agentId !== caller.agent.agentId) {
            throw new Error("ordinary members may read only their own pending context");
          }
          store.getAgent(workspace.workspaceId, agentId);
          return projectPendingContext(
            workspace.workspaceId,
            agentId,
            store.contextForAgent(workspace.workspaceId, agentId, true, optionalVersion(args.throughVersion)),
          );
        }
        case "send_message": {
          requireLeader(caller);
          const agent = requireTargetAgent(store, workspace, args.agentId, "agentId");
          const result = await executeAction({
            action: "send-agent",
            workspaceId: workspace.workspaceId,
            agentId: agent.agentId,
            content: requiredText(args.content, "content", MAX_MESSAGE),
          });
          onChange?.(workspace.workspaceId);
          return actionResult("send_message", workspace.workspaceId, result, {
            agentId: agent.agentId,
            accepted: result.delivery?.accepted ?? false,
          });
        }
        case "deliver_context": {
          requireLeader(caller);
          const agent = requireTargetAgent(store, workspace, args.agentId, "agentId");
          const result = await executeAction({
            action: "deliver-context",
            workspaceId: workspace.workspaceId,
            agentId: agent.agentId,
            throughVersion: optionalVersion(args.throughVersion),
          });
          onChange?.(workspace.workspaceId);
          return actionResult("deliver_context", workspace.workspaceId, result, {
            agentId: agent.agentId,
            accepted: result.delivery?.accepted ?? false,
            deliveredVersions: result.delivery?.deliveredVersions ?? [],
            ...(result.delivery?.reason === undefined ? {} : { reason: result.delivery.reason }),
          });
        }
        case "progress": {
          const explicitTarget = optionalText(args.targetAgentId, "targetAgentId");
          const targetAgentId = explicitTarget ?? workspace.leaderAgentId ?? caller.agent.agentId;
          if (!caller.isLeader) {
            const allowedTarget = workspace.leaderAgentId ?? caller.agent.agentId;
            if (targetAgentId !== allowedTarget) {
              throw new Error("ordinary members may submit progress only to the workspace Leader");
            }
          }
          const target = store.getAgent(workspace.workspaceId, targetAgentId);
          const result = await executeAction({
            action: "add-context",
            workspaceId: workspace.workspaceId,
            summary: requiredText(args.summary, "summary", MAX_PROGRESS),
            sourceRef: "visible-team:model-progress",
            createdBy: caller.agent.agentId,
            targets: [target.agentId],
          });
          onChange?.(workspace.workspaceId);
          return actionResult("progress", workspace.workspaceId, result, {
            targetAgentId: target.agentId,
            pendingContext: result.workspaces[0]?.agents.find(agent => agent.agentId === target.agentId)?.pendingContext,
          });
        }
      }
    },
    presentCall: args => ({
      card: "generic",
      title: `Visible Team · ${String(args.operation).replaceAll("_", " ")}`,
      kind: operationKind(args.operation),
      rawInput: args.agentId ?? args.targetAgentId ?? undefined,
    }),
    presentResult: (_args, _result) => ({ card: "generic" }),
  });
}

/** Register exactly one model tool through the official DSH ToolRuntime. */
export function registerVisibleTeamModelTool(
  tools: ToolRuntimeLike,
  store: VisibleTeamStore,
  executeAction: ModelActionExecutor,
  onChange?: (workspaceId: string) => void,
): () => void {
  return tools.register(createVisibleTeamModelTool(store, executeAction, onChange));
}
