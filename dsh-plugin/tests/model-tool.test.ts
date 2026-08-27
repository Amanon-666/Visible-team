import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { validateJsonSchemaValue } from "@deepseek-ai/dsh-tools";
import { afterEach, describe, expect, it } from "vitest";
import {
  createVisibleTeamModelTool,
  registerVisibleTeamModelTool,
  type ModelActionExecutor,
  type ToolRuntimeLike,
  type VisibleTeamToolResult,
} from "../src/host/model-tool.js";
import { VisibleTeamStore } from "../src/host/store.js";
import type { TeamAgent, WorkspaceAction, WorkspaceCommandResult } from "../src/shared/types.js";

const stores: VisibleTeamStore[] = [];
const temporary: string[] = [];

interface Fixture {
  store: VisibleTeamStore;
  workspaceId: string;
  leader: TeamAgent;
  member: TeamAgent;
  actions: WorkspaceAction[];
  sent: { agentId: string; content: string }[];
  changes: string[];
  tool: ReturnType<typeof createVisibleTeamModelTool>;
}

function fixture(): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "visible-team-model-tool-"));
  temporary.push(directory);
  const store = new VisibleTeamStore({ statePath: join(directory, "state.sqlite") });
  stores.push(store);
  const created = store.dispatch({
    action: "create-workspace",
    title: "模型协作空间",
    objective: "用最少上下文完成协作目标",
    sharedRules: "只向明确目标投递",
  });
  const withLeader = store.dispatch({
    action: "attach-agent",
    workspaceId: created.workspaceId,
    displayName: "DSH Leader",
    binding: { provider: "dsh", nativeSessionId: "leader-session" },
    asLeader: true,
  });
  const leader = withLeader.agents.find(agent => agent.binding.nativeSessionId === "leader-session") as TeamAgent;
  const withMember = store.dispatch({
    action: "attach-agent",
    workspaceId: created.workspaceId,
    displayName: "DSH Member",
    binding: { provider: "dsh", nativeSessionId: "member-session" },
  });
  const member = withMember.agents.find(agent => agent.binding.nativeSessionId === "member-session") as TeamAgent;
  const actions: WorkspaceAction[] = [];
  const sent: { agentId: string; content: string }[] = [];
  const executeAction: ModelActionExecutor = async action => {
    actions.push(action);
    if (action.action === "send-agent") {
      sent.push({ agentId: action.agentId, content: action.content });
      return {
        workspaces: [store.snapshot(action.workspaceId)],
        delivery: { action: "send-agent", agentId: action.agentId, accepted: true },
      };
    }
    if (action.action === "deliver-context") {
      const packets = store.contextForAgent(action.workspaceId, action.agentId, true, action.throughVersion);
      if (packets.length === 0) {
        return {
          workspaces: [store.snapshot(action.workspaceId)],
          delivery: { action: "deliver-context", agentId: action.agentId, accepted: false, deliveredVersions: [], reason: "no-pending-context" },
        };
      }
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
          accepted: true,
          deliveredVersions: packets.map(packet => packet.version),
        },
      };
    }
    if (action.action === "create-agent") throw new Error("model tool must not create agents");
    return { workspaces: [store.dispatch(action)] };
  };
  const changes: string[] = [];
  return {
    store,
    workspaceId: created.workspaceId,
    leader,
    member,
    actions,
    sent,
    changes,
    tool: createVisibleTeamModelTool(store, executeAction, workspaceId => changes.push(workspaceId)),
  };
}

async function call(
  tool: ReturnType<typeof createVisibleTeamModelTool>,
  args: Record<string, unknown>,
  nativeSessionId: string,
): Promise<VisibleTeamToolResult> {
  return await tool.execute(args, { agent: { id: nativeSessionId } } as never) as VisibleTeamToolResult;
}

function expectValidResult(tool: ReturnType<typeof createVisibleTeamModelTool>, value: VisibleTeamToolResult): void {
  expect(validateJsonSchemaValue(tool.output.schema, value)).toEqual([]);
}

afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
});

describe("Visible Team model tool contract", () => {
  it("registers exactly one official tool with a compact, explicit surface", () => {
    const current = fixture();
    const registered: ReturnType<typeof createVisibleTeamModelTool>[] = [];
    const tools: ToolRuntimeLike = {
      register(definition) {
        registered.push(definition);
        return () => undefined;
      },
    };
    const dispose = registerVisibleTeamModelTool(tools, current.store, async action => ({
      workspaces: [current.store.snapshot(action.workspaceId)],
    }));

    expect(registered).toHaveLength(1);
    const definition = registered[0] as ReturnType<typeof createVisibleTeamModelTool>;
    expect(definition.name).toBe("visible_team");
    expect(definition.parameters).toMatchObject({
      properties: { operation: { type: "string" } },
      required: ["operation"],
    });
    const parameterProperties = (definition.parameters as { properties: Record<string, unknown> }).properties;
    expect(Object.keys(parameterProperties)).toEqual(expect.arrayContaining([
      "operation", "workspaceId", "agentId", "content", "throughVersion", "summary", "targetAgentId",
    ]));
    expect(Object.keys(parameterProperties)).not.toEqual(expect.arrayContaining([
      "createAgent", "permissionMode", "role", "provider",
    ]));
    expect(definition.output.schema).toMatchObject({ type: "object", additionalProperties: false });
    expect(definition.presentCall?.({ operation: "send_message", agentId: current.member.agentId })).toMatchObject({
      card: "generic",
      kind: "other",
    });
    expect(definition.presentResult?.({ operation: "send_message" }, { content: [], isError: false })).toEqual({ card: "generic" });
    dispose();
  });

  it("fails closed when the DSH identity is absent, unbound, non-DSH, or mismatched", async () => {
    const current = fixture();
    await expect(call(current.tool, { operation: "read_workspace", workspaceId: current.workspaceId }, "unattached-session"))
      .rejects.toThrow(/not attached/);
    await expect(call(current.tool, { operation: "read_workspace", workspaceId: "another-space" }, "leader-session"))
      .rejects.toThrow(/must match/);
    await expect(call(current.tool, { operation: "read_workspace", workspaceId: current.workspaceId }, ""))
      .rejects.toThrow(/stable session id/);

    current.store.dispatch({
      action: "attach-agent",
      workspaceId: current.workspaceId,
      displayName: "Codex session",
      binding: { provider: "codex", nativeSessionId: "codex-session" },
    });
    await expect(call(current.tool, { operation: "progress", summary: "不应跨 provider 放行" }, "codex-session"))
      .rejects.toThrow(/not attached/);
  });

  it("limits ordinary members to their own pending context and targeted progress", async () => {
    const current = fixture();
    const ownContext = await call(current.tool, { operation: "read_pending_context" }, "member-session");
    expectValidResult(current.tool, ownContext);
    expect(ownContext).toMatchObject({ kind: "pending-context", agentId: current.member.agentId, pendingCount: 1 });

    await expect(call(current.tool, { operation: "list_workspaces" }, "member-session"))
      .rejects.toThrow(/only the workspace Leader/);
    await expect(call(current.tool, { operation: "read_pending_context", agentId: current.leader.agentId }, "member-session"))
      .rejects.toThrow(/only their own/);
    await expect(call(current.tool, { operation: "send_message", agentId: current.leader.agentId, content: "越权" }, "member-session"))
      .rejects.toThrow(/only the workspace Leader/);
    await expect(call(current.tool, { operation: "progress", targetAgentId: current.member.agentId, summary: "错误目标" }, "member-session"))
      .rejects.toThrow(/only to the workspace Leader/);

    const progress = await call(current.tool, { operation: "progress", summary: "成员已完成本轮验证" }, "member-session");
    expectValidResult(current.tool, progress);
    expect(progress).toMatchObject({
      kind: "action",
      operation: "progress",
      workspaceId: current.workspaceId,
      targetAgentId: current.leader.agentId,
    });
    expect(current.actions.at(-1)).toMatchObject({
      action: "add-context",
      workspaceId: current.workspaceId,
      createdBy: current.member.agentId,
      targets: [current.leader.agentId],
      sourceRef: "visible-team:model-progress",
    });
    expect(current.changes).toEqual([current.workspaceId]);
  });

  it("lets the bound Leader inspect short projections and use existing send/deliver actions", async () => {
    const current = fixture();
    const workspace = await call(current.tool, { operation: "list_workspaces" }, "leader-session");
    expectValidResult(current.tool, workspace);
    expect(workspace).toMatchObject({ kind: "workspace-list", workspaceId: current.workspaceId, agentCount: 2 });
    expect(workspace).not.toHaveProperty("context");
    expect(workspace).not.toHaveProperty("sharedRules");

    current.store.dispatch({
      action: "update-workspace",
      workspaceId: current.workspaceId,
      objective: "目标".repeat(300),
    });
    const boundedWorkspace = await call(current.tool, { operation: "read_workspace" }, "leader-session");
    expectValidResult(current.tool, boundedWorkspace);
    expect(boundedWorkspace).toMatchObject({ kind: "workspace", truncated: true });

    const giant = "上下文摘要".repeat(500);
    current.store.dispatch({
      action: "add-context",
      workspaceId: current.workspaceId,
      summary: giant,
      targets: [current.member.agentId],
    });
    const pending = await call(current.tool, {
      operation: "read_pending_context",
      agentId: current.member.agentId,
    }, "leader-session");
    expectValidResult(current.tool, pending);
    expect(pending).toMatchObject({ kind: "pending-context", agentId: current.member.agentId, truncated: true });
    if (pending.kind !== "pending-context") throw new Error("expected pending context result");
    expect(pending.pending.length).toBeLessThanOrEqual(8);
    expect(pending.pending.some(packet => packet.summary.length <= 1_000)).toBe(true);
    expect(pending.pending.some(packet => packet.summary.endsWith("…"))).toBe(true);

    const sent = await call(current.tool, {
      operation: "send_message",
      agentId: current.member.agentId,
      content: "请继续执行已确认的步骤",
    }, "leader-session");
    expectValidResult(current.tool, sent);
    expect(sent).toMatchObject({ kind: "action", operation: "send_message", agentId: current.member.agentId, accepted: true });
    expect(current.sent).toEqual([{ agentId: current.member.agentId, content: "请继续执行已确认的步骤" }]);

    const delivered = await call(current.tool, {
      operation: "deliver_context",
      agentId: current.member.agentId,
    }, "leader-session");
    expectValidResult(current.tool, delivered);
    expect(delivered).toMatchObject({ kind: "action", operation: "deliver_context", agentId: current.member.agentId, accepted: true });
    expect(current.store.contextForAgent(current.workspaceId, current.member.agentId)).toHaveLength(0);
    expect(current.actions.filter(action => action.action === "send-agent")).toHaveLength(1);
    expect(current.changes).toEqual([current.workspaceId, current.workspaceId]);
  });
});
