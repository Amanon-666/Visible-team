import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";
import { VisibleTeamStore } from "../src/host/store.js";
import type { WorkspaceAction } from "../src/shared/types.js";

const temporary: string[] = [];
const stores: VisibleTeamStore[] = [];

function store(): VisibleTeamStore {
  const directory = mkdtempSync(join(tmpdir(), "visible-team-store-"));
  temporary.push(directory);
  const next = new VisibleTeamStore({ statePath: join(directory, "state.sqlite") });
  stores.push(next);
  return next;
}

function createWorkspace(db: VisibleTeamStore, title = "目标空间") {
  return db.dispatch({ action: "create-workspace", title, objective: "完成一个跨平台目标", sharedRules: "先验证，再汇报", hostBinding: { kind: "opaque-host", ref: "host-workspace-1" } });
}

function attach(db: VisibleTeamStore, workspaceId: string, provider: string, nativeSessionId: string) {
  return db.dispatch({
    action: "attach-agent",
    workspaceId,
    displayName: `${provider}-${nativeSessionId}`,
    binding: { provider, nativeSessionId },
    attachSource: "manual",
  });
}

function legacyStore(): { db: VisibleTeamStore; workspaceId: string; firstAgentId: string; secondAgentId: string } {
  const directory = mkdtempSync(join(tmpdir(), "visible-team-legacy-store-"));
  temporary.push(directory);
  const statePath = join(directory, "state.sqlite");
  const raw = new DatabaseSync(statePath);
  raw.exec(`
    PRAGMA foreign_keys = ON;
    CREATE TABLE vt_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE vt_workspaces (
      workspace_id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      objective TEXT NOT NULL,
      shared_rules TEXT NOT NULL DEFAULT '',
      dsh_workspace_id TEXT,
      leader_agent_id TEXT,
      status TEXT NOT NULL,
      version INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE vt_agents (
      agent_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      display_name TEXT NOT NULL,
      provider TEXT NOT NULL,
      native_session_id TEXT NOT NULL,
      native_open_ref TEXT,
      model TEXT,
      thinking TEXT,
      permission_mode TEXT,
      responsibility TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      attach_source TEXT NOT NULL,
      context_version INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (workspace_id, provider, native_session_id),
      FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE
    );
    CREATE TABLE vt_context_updates (
      update_id INTEGER PRIMARY KEY AUTOINCREMENT,
      workspace_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      summary TEXT NOT NULL,
      source_ref TEXT,
      created_by TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (workspace_id, version),
      FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE
    );
    CREATE TABLE vt_context_targets (
      update_id INTEGER NOT NULL,
      agent_id TEXT NOT NULL,
      delivered_at TEXT,
      PRIMARY KEY (update_id, agent_id),
      FOREIGN KEY (update_id) REFERENCES vt_context_updates(update_id) ON DELETE CASCADE,
      FOREIGN KEY (agent_id) REFERENCES vt_agents(agent_id) ON DELETE CASCADE
    );
    CREATE TABLE vt_usage (
      usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
      workspace_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      observation_id TEXT NOT NULL,
      source TEXT NOT NULL,
      input_tokens INTEGER,
      cached_input_tokens INTEGER,
      output_tokens INTEGER,
      reasoning_output_tokens INTEGER,
      total_tokens INTEGER,
      observed_at TEXT NOT NULL,
      UNIQUE (observation_id),
      FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE,
      FOREIGN KEY (agent_id) REFERENCES vt_agents(agent_id) ON DELETE CASCADE
    );
  `);
  const timestamp = "2026-08-28T00:00:00.000Z";
  raw.prepare(`
    INSERT INTO vt_workspaces(
      workspace_id, title, objective, shared_rules, dsh_workspace_id,
      leader_agent_id, status, version, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, NULL, 'active', 0, ?, ?)
  `).run("legacy-space", "Legacy space", "Keep the old data", "Use native counts", "legacy-dir", timestamp, timestamp);
  const insertAgent = raw.prepare(`
    INSERT INTO vt_agents(
      agent_id, workspace_id, display_name, provider, native_session_id,
      native_open_ref, model, thinking, permission_mode, responsibility,
      status, attach_source, context_version, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, '', 'active', 'manual', 0, ?, ?)
  `);
  insertAgent.run("legacy-agent-a", "legacy-space", "Agent A", "provider", "session-a", timestamp, timestamp);
  insertAgent.run("legacy-agent-b", "legacy-space", "Agent B", "provider", "session-b", timestamp, timestamp);
  raw.prepare(`
    INSERT INTO vt_usage(
      workspace_id, agent_id, observation_id, source, input_tokens,
      cached_input_tokens, output_tokens, reasoning_output_tokens,
      total_tokens, observed_at
    ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
  `).run("legacy-space", "legacy-agent-a", "turn-1", "provider-native", 3, 2, 5, timestamp);
  raw.close();

  const db = new VisibleTeamStore({ statePath });
  stores.push(db);
  return { db, workspaceId: "legacy-space", firstAgentId: "legacy-agent-a", secondAgentId: "legacy-agent-b" };
}

afterEach(() => {
  for (const db of stores.splice(0)) db.close();
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
});

describe("VisibleTeamStore", () => {
  it("keeps host bindings opaque and enforces one native identity owner", () => {
    const db = store();
    const first = createWorkspace(db);
    expect(first.hostBinding).toEqual({ kind: "opaque-host", ref: "host-workspace-1" });
    expect("dshWorkspaceId" in first).toBe(false);

    const withAgent = attach(db, first.workspaceId, "dsh", "session-1");
    const agent = withAgent.agents[0];
    expect(agent?.binding).toEqual({ provider: "dsh", nativeSessionId: "session-1", nativeOpenRef: null });
    expect(agent?.pendingContext).toBe(1);
    expect(withAgent.context).toHaveLength(1);
    expect(withAgent.context[0]).toMatchObject({ targets: [agent?.agentId], deliveredAt: null, sourceRef: "visible-team:workspace-bootstrap" });
    expect(withAgent.context[0]?.summary).toContain("完成一个跨平台目标");
    expect(withAgent.context[0]?.summary).toContain("先验证，再汇报");
    const reattached = attach(db, first.workspaceId, "dsh", "session-1");
    expect(reattached.context).toHaveLength(1);

    const second = createWorkspace(db, "另一个空间");
    expect(() => attach(db, second.workspaceId, "dsh", "session-1")).toThrow(/already attached/);
    expect(() => db.dispatch({ action: "update-workspace", workspaceId: second.workspaceId, leaderAgentId: agent?.agentId ?? "missing" })).toThrow(/unknown agent/);
    expect(() => db.dispatch({
      action: "attach-agent",
      workspaceId: first.workspaceId,
      displayName: "伪造新 Agent",
      binding: { provider: "dsh", nativeSessionId: "session-2" },
      attachSource: "created" as "manual",
    })).toThrow(/connected driver/);
  });

  it("persists an explicit native model provider without changing the driver identity", () => {
    const db = store();
    const workspace = createWorkspace(db);
    const result = db.dispatch({
      action: "attach-agent",
      workspaceId: workspace.workspaceId,
      displayName: "Routed DSH",
      nativeProvider: "deepseek-official",
      model: "deepseek-chat",
      thinking: "high",
      permissionMode: "safe",
      binding: { provider: "dsh", nativeSessionId: "session-routed" },
      attachSource: "manual",
    });
    expect(result.agents[0]?.binding).toEqual({
      provider: "dsh",
      nativeProvider: "deepseek-official",
      nativeSessionId: "session-routed",
      nativeOpenRef: null,
    });
  });

  it("stores context packets with transactionally increasing versions and explicit targets", () => {
    const db = store();
    const workspace = createWorkspace(db);
    const first = attach(db, workspace.workspaceId, "dsh", "session-a");
    const second = attach(db, workspace.workspaceId, "codex", "task-b");
    const firstId = first.agents[0]?.agentId as string;
    const secondId = second.agents.find(agent => agent.binding.nativeSessionId === "task-b")?.agentId as string;
    expect(second.context.every(packet => packet.targets.length === 1)).toBe(true);
    expect(second.context.map(packet => packet.targets)).toEqual(expect.arrayContaining([[firstId], [secondId]]));
    expect(second.context.find(packet => packet.targets[0] === secondId)?.summary).toContain("完成一个跨平台目标");

    // Explicitly deliver each new Agent's one-time bootstrap before testing
    // later increments; the store never auto-delivers it.
    db.dispatch({ action: "ack-context", workspaceId: workspace.workspaceId, agentId: firstId, throughVersion: 1 });
    db.dispatch({ action: "ack-context", workspaceId: workspace.workspaceId, agentId: secondId, throughVersion: 2 });

    const packet1 = db.dispatch({ action: "add-context", workspaceId: workspace.workspaceId, summary: "只给 A 的增量", targets: [firstId] });
    const packet2 = db.dispatch({ action: "add-context", workspaceId: workspace.workspaceId, summary: "只给 B 的增量", targets: [secondId] });
    expect(packet2.version).toBe(packet1.version + 1);
    expect(db.contextForAgent(workspace.workspaceId, firstId)).toHaveLength(1);
    expect(db.contextForAgent(workspace.workspaceId, secondId)).toHaveLength(1);
    expect(db.contextForAgent(workspace.workspaceId, firstId)[0]?.summary).toBe("只给 A 的增量");
    expect(db.contextForAgent(workspace.workspaceId, secondId)[0]?.summary).toBe("只给 B 的增量");
    expect(() => db.dispatch({ action: "add-context", workspaceId: workspace.workspaceId, summary: "广播不允许", targets: ["all"] })).toThrow(/targets=all|broadcast/);

    const acked = db.dispatch({ action: "ack-context", workspaceId: workspace.workspaceId, agentId: firstId, throughVersion: packet1.version });
    expect(acked.agents.find(agent => agent.agentId === firstId)?.pendingContext).toBe(0);
    expect(acked.agents.find(agent => agent.agentId === firstId)?.contextVersion).toBe(packet1.version);
    expect(acked.agents.find(agent => agent.agentId === secondId)?.pendingContext).toBe(1);
    expect(db.contextForAgent(workspace.workspaceId, firstId)).toHaveLength(0);
    expect(db.contextForAgent(workspace.workspaceId, secondId)).toHaveLength(1);
    expect(db.contextForAgent(workspace.workspaceId, firstId, false)[0]?.targets).toEqual([firstId]);
  });

  it("does not sum cumulative snapshots or estimate missing native totals", () => {
    const db = store();
    const workspace = createWorkspace(db);
    const result = attach(db, workspace.workspaceId, "codex", "task-usage");
    const agentId = result.agents[0]?.agentId as string;
    const cumulative = (observationId: string, totalTokens: number): WorkspaceAction => ({
      action: "record-usage",
      workspaceId: workspace.workspaceId,
      agentId,
      observationId,
      source: "codex-native",
      accounting: "cumulative",
      inputTokens: totalTokens - 2,
      outputTokens: 2,
      totalTokens,
    });
    db.dispatch(cumulative("obs-1", 10));
    db.dispatch(cumulative("obs-2", 15));
    const replay = db.dispatch(cumulative("obs-2", 15));
    expect(replay.agents[0]?.usage.totalTokens).toBe(15);
    expect(replay.agents[0]?.usage.latestObservationId).toBe("obs-2");

    const missingTotal = attach(db, workspace.workspaceId, "acp", "task-missing-total");
    const missingId = missingTotal.agents.find(agent => agent.binding.provider === "acp")?.agentId as string;
    const missing = db.dispatch({
      action: "record-usage",
      workspaceId: workspace.workspaceId,
      agentId: missingId,
      observationId: "obs-missing-total",
      source: "acp-native",
      accounting: "delta",
      inputTokens: 8,
      outputTokens: 4,
    });
    expect(missing.agents.find(agent => agent.agentId === missingId)?.usage.totalTokens).toBeNull();
  });

  it("scopes observation idempotency by Agent and source when migrating the old global key", () => {
    const legacy = legacyStore();
    expect(legacy.db.snapshot(legacy.workspaceId).hostBinding).toEqual({ kind: "legacy-host", ref: "legacy-dir" });

    const record = (agentId: string, totalTokens: number): WorkspaceAction => ({
      action: "record-usage",
      workspaceId: legacy.workspaceId,
      agentId,
      observationId: "turn-1",
      source: "provider-native",
      accounting: "delta",
      inputTokens: totalTokens - 2,
      outputTokens: 2,
      totalTokens,
    });
    const afterSecond = legacy.db.dispatch(record(legacy.secondAgentId, 9));
    expect(afterSecond.agents.find(agent => agent.agentId === legacy.firstAgentId)?.usage.totalTokens).toBe(5);
    expect(afterSecond.agents.find(agent => agent.agentId === legacy.secondAgentId)?.usage.totalTokens).toBe(9);

    const replay = legacy.db.dispatch(record(legacy.firstAgentId, 5));
    expect(replay.agents.find(agent => agent.agentId === legacy.firstAgentId)?.usage.latestObservationId).toBe("turn-1");
    expect(replay.agents.find(agent => agent.agentId === legacy.secondAgentId)?.usage.latestObservationId).toBe("turn-1");
  });
});
