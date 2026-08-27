import { chmodSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import type {
  AgentAttachSource,
  AgentBinding,
  AgentStatus,
  AgentUsageSummary,
  ContextPacket,
  HostBinding,
  TeamAgent,
  TeamWorkspace,
  UsageAccounting,
  WorkspaceAction,
  WorkspaceStateAction,
  WorkspaceStatus,
} from "../shared/types.js";

type Row = Record<string, unknown>;

export interface StoreConfig {
  statePath?: string;
}

const WORKSPACE_STATUSES = new Set<WorkspaceStatus>(["active", "paused", "completed", "cancelled"]);
const AGENT_STATUSES = new Set<AgentStatus>([
  "planned", "active", "idle", "waiting", "blocked", "completed", "failed", "cancelled",
]);
const ATTACH_SOURCES = new Set<AgentAttachSource>(["created", "manual", "discovered"]);
const USAGE_ACCOUNTING = new Set<UsageAccounting>(["delta", "cumulative"]);

function now(): string {
  return new Date().toISOString();
}

function cleanText(value: unknown, field: string, options: { required?: boolean; max?: number } = {}): string {
  if (typeof value !== "string") {
    if (options.required) throw new Error(`${field} is required`);
    return "";
  }
  const result = value.trim();
  if (options.required && !result) throw new Error(`${field} is required`);
  if (result.length > (options.max ?? 20_000)) throw new Error(`${field} is too long`);
  return result;
}

function nullableText(value: unknown, field: string, max = 20_000): string | null {
  const result = cleanText(value, field, { max });
  return result || null;
}

function optionalCount(value: unknown, field: string): number | null {
  if (value === undefined || value === null) return null;
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error(`${field} must be a non-negative integer`);
  return Number(value);
}

function requiredCount(value: unknown, field: string): number {
  const result = optionalCount(value, field);
  if (result === null) throw new Error(`${field} is required`);
  return result;
}

function defaultStatePath(): string {
  const dshHome = process.env.DSH_HOME?.trim() || join(homedir(), ".dsh");
  return join(dshHome, "visible-team", "workspace.sqlite");
}

function tableColumns(db: DatabaseSync, table: string): Set<string> {
  return new Set((db.prepare(`PRAGMA table_info(${table})`).all() as Row[]).map(row => String(row.name)));
}

function addColumn(db: DatabaseSync, table: string, column: string, definition: string): void {
  if (!tableColumns(db, table).has(column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
}

function indexColumns(db: DatabaseSync, indexName: string): string[] {
  // Names come from SQLite's PRAGMA output, never from a request. Keep the
  // identifier check anyway because PRAGMA does not accept bound parameters.
  if (!/^[A-Za-z0-9_]+$/.test(indexName)) return [];
  return (db.prepare(`PRAGMA index_info("${indexName}")`).all() as Row[])
    .sort((left, right) => Number(left.seq) - Number(right.seq))
    .map(row => String(row.name));
}

function hasUniqueIndex(db: DatabaseSync, table: string, expectedColumns: readonly string[]): boolean {
  return (db.prepare(`PRAGMA index_list(${table})`).all() as Row[]).some(row =>
    Number(row.unique) === 1 && JSON.stringify(indexColumns(db, String(row.name))) === JSON.stringify(expectedColumns),
  );
}

function parseHostBinding(row: Row): HostBinding | null {
  const kind = row.host_binding_kind;
  const ref = row.host_binding_ref;
  return kind === null || kind === undefined || ref === null || ref === undefined
    ? null
    : { kind: String(kind), ref: String(ref) };
}

function parseAgentBinding(row: Row): AgentBinding {
  return {
    provider: String(row.provider),
    nativeSessionId: String(row.native_session_id),
    nativeOpenRef: row.native_open_ref === null ? null : String(row.native_open_ref),
  };
}

function sumKnown(values: readonly (number | null)[]): number | null {
  if (values.length === 0) return null;
  let total = 0;
  for (const value of values) {
    if (value === null) return null;
    total += value;
  }
  return total;
}

function numberOrNull(value: unknown): number | null {
  return value === null || value === undefined ? null : Number(value);
}

function bootstrapSummary(objective: string, sharedRules: string): string {
  const sections = [`Workspace objective:\n${objective}`];
  if (sharedRules) sections.push(`Shared rules:\n${sharedRules}`);
  return sections.join("\n\n");
}

export class VisibleTeamStore {
  private readonly db: DatabaseSync;

  constructor(config: StoreConfig = {}) {
    const statePath = resolve(config.statePath?.trim() || process.env.VISIBLE_TEAM_STATE_PATH?.trim() || defaultStatePath());
    mkdirSync(dirname(statePath), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(statePath);
    try { chmodSync(statePath, 0o600); } catch { /* Windows and inherited files may not expose POSIX modes. */ }
    this.db.exec("PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000; PRAGMA journal_mode = WAL;");
    this.migrate();
  }

  close(): void {
    this.db.close();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS vt_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      ) STRICT;
      CREATE TABLE IF NOT EXISTS vt_workspaces (
        workspace_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        objective TEXT NOT NULL,
        shared_rules TEXT NOT NULL DEFAULT '',
        host_binding_kind TEXT,
        host_binding_ref TEXT,
        leader_agent_id TEXT,
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      ) STRICT;
      CREATE TABLE IF NOT EXISTS vt_agents (
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
      ) STRICT;
      CREATE TABLE IF NOT EXISTS vt_context_updates (
        update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        summary TEXT NOT NULL,
        source_ref TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (workspace_id, version),
        FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE
      ) STRICT;
      CREATE TABLE IF NOT EXISTS vt_context_targets (
        update_id INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        delivered_at TEXT,
        PRIMARY KEY (update_id, agent_id),
        FOREIGN KEY (update_id) REFERENCES vt_context_updates(update_id) ON DELETE CASCADE,
        FOREIGN KEY (agent_id) REFERENCES vt_agents(agent_id) ON DELETE CASCADE
      ) STRICT;
      CREATE TABLE IF NOT EXISTS vt_usage (
        usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        observation_id TEXT NOT NULL,
        source TEXT NOT NULL,
        accounting TEXT NOT NULL,
        input_tokens INTEGER,
        cached_input_tokens INTEGER,
        output_tokens INTEGER,
        reasoning_output_tokens INTEGER,
        total_tokens INTEGER,
        observed_at TEXT NOT NULL,
        UNIQUE (agent_id, source, observation_id),
        FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE,
        FOREIGN KEY (agent_id) REFERENCES vt_agents(agent_id) ON DELETE CASCADE
      ) STRICT;
      CREATE INDEX IF NOT EXISTS vt_context_target_pending_idx
        ON vt_context_targets(agent_id, delivered_at);
      CREATE INDEX IF NOT EXISTS vt_usage_agent_idx
        ON vt_usage(workspace_id, agent_id, usage_id);
    `);

    // The initial scaffold used dsh_workspace_id and did not fully scope
    // usage observations. Keep existing state readable without retaining
    // that host-specific name in the core contract.
    addColumn(this.db, "vt_workspaces", "host_binding_kind", "TEXT");
    addColumn(this.db, "vt_workspaces", "host_binding_ref", "TEXT");
    const workspaceColumns = tableColumns(this.db, "vt_workspaces");
    if (workspaceColumns.has("dsh_workspace_id")) {
      this.db.exec(`
        UPDATE vt_workspaces
        SET host_binding_kind = COALESCE(host_binding_kind, 'legacy-host'),
            host_binding_ref = COALESCE(host_binding_ref, dsh_workspace_id)
        WHERE dsh_workspace_id IS NOT NULL
      `);
    }
    addColumn(this.db, "vt_usage", "observation_id", "TEXT");
    addColumn(this.db, "vt_usage", "accounting", "TEXT NOT NULL DEFAULT 'delta'");
    this.db.exec("UPDATE vt_usage SET observation_id = 'legacy-' || usage_id WHERE observation_id IS NULL");
    this.migrateUsageObservationScope();

    // A native session/task is a single externally-owned resource. This
    // global fence prevents one provider session from being attached to two
    // collaboration spaces and therefore acquiring two bridge owners.
    this.db.exec("CREATE UNIQUE INDEX IF NOT EXISTS vt_agent_native_owner_idx ON vt_agents(provider, native_session_id)");
    this.db.prepare("INSERT OR IGNORE INTO vt_meta(key, value) VALUES ('schema_version', '4')").run();
    this.db.prepare("UPDATE vt_meta SET value = '4' WHERE key = 'schema_version'").run();
  }

  /**
   * The first usage schema made observation_id globally unique. Rebuild that
   * table when the old inline constraint or migration index is present; an
   * index cannot be dropped when it was created by a table-level UNIQUE.
   */
  private migrateUsageObservationScope(): void {
    const scopedColumns = ["agent_id", "source", "observation_id"] as const;
    if (hasUniqueIndex(this.db, "vt_usage", scopedColumns)) return;
    if (!hasUniqueIndex(this.db, "vt_usage", ["observation_id"])) {
      this.db.exec("CREATE UNIQUE INDEX IF NOT EXISTS vt_usage_observation_scope_idx ON vt_usage(agent_id, source, observation_id)");
      return;
    }

    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db.exec(`
        ALTER TABLE vt_usage RENAME TO vt_usage_legacy;
        CREATE TABLE vt_usage_new (
          usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
          workspace_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          observation_id TEXT NOT NULL,
          source TEXT NOT NULL,
          accounting TEXT NOT NULL,
          input_tokens INTEGER,
          cached_input_tokens INTEGER,
          output_tokens INTEGER,
          reasoning_output_tokens INTEGER,
          total_tokens INTEGER,
          observed_at TEXT NOT NULL,
          UNIQUE (agent_id, source, observation_id),
          FOREIGN KEY (workspace_id) REFERENCES vt_workspaces(workspace_id) ON DELETE CASCADE,
          FOREIGN KEY (agent_id) REFERENCES vt_agents(agent_id) ON DELETE CASCADE
        ) STRICT;
        INSERT INTO vt_usage_new(
          usage_id, workspace_id, agent_id, observation_id, source, accounting,
          input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
          total_tokens, observed_at
        )
        SELECT usage_id, workspace_id, agent_id, observation_id, source, accounting,
               input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
               total_tokens, observed_at
        FROM vt_usage_legacy;
        DROP TABLE vt_usage_legacy;
        ALTER TABLE vt_usage_new RENAME TO vt_usage;
        CREATE INDEX vt_usage_context_agent_idx ON vt_usage(workspace_id, agent_id, usage_id);
      `);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  private workspaceRow(workspaceId: string): Row {
    const row = this.db.prepare("SELECT * FROM vt_workspaces WHERE workspace_id = ?").get(workspaceId) as Row | undefined;
    if (!row) throw new Error(`unknown workspace: ${workspaceId}`);
    return row;
  }

  private agentRow(workspaceId: string, agentId: string): Row {
    const row = this.db.prepare("SELECT * FROM vt_agents WHERE workspace_id = ? AND agent_id = ?").get(workspaceId, agentId) as Row | undefined;
    if (!row) throw new Error(`unknown agent: ${agentId}`);
    return row;
  }

  getAgent(workspaceId: string, agentId: string): TeamAgent {
    return this.mapAgent(this.agentRow(workspaceId, agentId));
  }

  listWorkspaces(): TeamWorkspace[] {
    const rows = this.db.prepare("SELECT * FROM vt_workspaces ORDER BY updated_at DESC, workspace_id").all() as Row[];
    return rows.map(row => this.snapshot(String(row.workspace_id)));
  }

  snapshot(workspaceId: string): TeamWorkspace {
    const row = this.workspaceRow(workspaceId);
    const agentRows = this.db.prepare(`
      SELECT a.*,
             SUM(CASE WHEN t.update_id IS NOT NULL AND t.delivered_at IS NULL THEN 1 ELSE 0 END) AS pending_context
      FROM vt_agents a
      LEFT JOIN vt_context_targets t ON t.agent_id = a.agent_id
      WHERE a.workspace_id = ?
      GROUP BY a.agent_id
      ORDER BY a.created_at, a.agent_id
    `).all(workspaceId) as Row[];
    const agents = agentRows.map(agent => this.mapAgent(agent));
    const contextRows = this.db.prepare(`
      SELECT u.*,
             COUNT(t.agent_id) AS target_count,
             SUM(CASE WHEN t.delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered_count,
             MAX(t.delivered_at) AS last_delivered_at
      FROM vt_context_updates u
      LEFT JOIN vt_context_targets t ON t.update_id = u.update_id
      WHERE u.workspace_id = ?
      GROUP BY u.update_id
      ORDER BY u.version DESC LIMIT 100
    `).all(workspaceId) as Row[];
    const context = contextRows.map(row => this.mapContext(row, false));
    return {
      workspaceId: String(row.workspace_id),
      title: String(row.title),
      objective: String(row.objective),
      sharedRules: String(row.shared_rules),
      hostBinding: parseHostBinding(row),
      leaderAgentId: row.leader_agent_id === null ? null : String(row.leader_agent_id),
      status: String(row.status) as WorkspaceStatus,
      version: Number(row.version),
      agents,
      context,
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at),
    };
  }

  /** A target-scoped read; no packet for another Agent is returned. */
  contextForAgent(workspaceId: string, agentId: string, pendingOnly = true, throughVersion?: number): ContextPacket[] {
    this.agentRow(workspaceId, agentId);
    const clauses = ["u.workspace_id = ?", "t.agent_id = ?"];
    const params: (string | number)[] = [workspaceId, agentId];
    if (pendingOnly) clauses.push("t.delivered_at IS NULL");
    if (throughVersion !== undefined) {
      if (!Number.isSafeInteger(throughVersion) || throughVersion < 0) throw new Error("throughVersion must be a non-negative integer");
      clauses.push("u.version <= ?");
      params.push(throughVersion);
    }
    const rows = this.db.prepare(`
      SELECT u.*, t.delivered_at AS target_delivered_at
      FROM vt_context_updates u
      JOIN vt_context_targets t ON t.update_id = u.update_id
      WHERE ${clauses.join(" AND ")}
      ORDER BY u.version
    `).all(...params) as Row[];
    return rows.map(row => this.mapContext(row, true, agentId));
  }

  private mapContext(row: Row, targetScoped: boolean, targetAgentId?: string): ContextPacket {
    const targets = targetScoped
      ? [targetAgentId as string]
      : this.contextTargets(Number(row.update_id));
    const targetCount = Number(row.target_count ?? targets.length);
    const deliveredCount = Number(row.delivered_count ?? (row.target_delivered_at === null ? 0 : 1));
    return {
      updateId: Number(row.update_id),
      workspaceId: String(row.workspace_id),
      version: Number(row.version),
      summary: String(row.summary),
      sourceRef: row.source_ref === null ? null : String(row.source_ref),
      createdBy: String(row.created_by),
      targets,
      deliveredAt: targetScoped
        ? (row.target_delivered_at === null ? null : String(row.target_delivered_at))
        : targetCount > 0 && targetCount === deliveredCount && row.last_delivered_at !== null
          ? String(row.last_delivered_at)
          : null,
      createdAt: String(row.created_at),
    };
  }

  private contextTargets(updateId: number): string[] {
    return (this.db.prepare(
      "SELECT agent_id FROM vt_context_targets WHERE update_id = ? ORDER BY agent_id",
    ).all(updateId) as Row[]).map(row => String(row.agent_id));
  }

  private mapAgent(row: Row): TeamAgent {
    const usage = this.usageSummary(String(row.workspace_id), String(row.agent_id));
    const status = String(row.status) as AgentStatus;
    if (!AGENT_STATUSES.has(status)) throw new Error(`invalid persisted agent status: ${status}`);
    const attachSource = String(row.attach_source) as AgentAttachSource;
    if (!ATTACH_SOURCES.has(attachSource)) throw new Error(`invalid persisted attach source: ${attachSource}`);
    return {
      agentId: String(row.agent_id),
      workspaceId: String(row.workspace_id),
      displayName: String(row.display_name),
      binding: parseAgentBinding(row),
      model: row.model === null ? null : String(row.model),
      thinking: row.thinking === null ? null : String(row.thinking),
      permissionMode: row.permission_mode === null ? null : String(row.permission_mode),
      responsibility: String(row.responsibility),
      status,
      attachSource,
      contextVersion: Number(row.context_version),
      pendingContext: Number(row.pending_context ?? 0),
      usage,
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at),
    };
  }

  private usageSummary(workspaceId: string, agentId: string): AgentUsageSummary {
    const rows = this.db.prepare(`
      SELECT * FROM vt_usage WHERE workspace_id = ? AND agent_id = ? ORDER BY usage_id
    `).all(workspaceId, agentId) as Row[];
    if (rows.length === 0) {
      return {
        inputTokens: null,
        cachedInputTokens: null,
        outputTokens: null,
        reasoningOutputTokens: null,
        totalTokens: null,
        accounting: null,
        latestObservationId: null,
      };
    }
    const streams = new Map<string, Row[]>();
    for (const row of rows) {
      const source = String(row.source);
      const stream = streams.get(source) ?? [];
      stream.push(row);
      streams.set(source, stream);
    }
    const fields = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"];
    const values = Object.fromEntries(fields.map(field => {
      const perSource = [...streams.values()].map(stream => {
        const accounting = String(stream[0]?.accounting ?? "delta") as UsageAccounting;
        if (accounting === "cumulative") {
          return numberOrNull(stream[stream.length - 1]?.[field]);
        }
        return sumKnown(stream.map(row => numberOrNull(row[field])));
      });
      return [field, sumKnown(perSource)];
    }));
    const accountings = [...streams.values()].map(stream => String(stream[0]?.accounting ?? "delta") as UsageAccounting);
    const accounting = accountings.every(value => value === accountings[0]) ? accountings[0] as UsageAccounting : null;
    const latest = rows[rows.length - 1];
    return {
      inputTokens: values.input_tokens as number | null,
      cachedInputTokens: values.cached_input_tokens as number | null,
      outputTokens: values.output_tokens as number | null,
      reasoningOutputTokens: values.reasoning_output_tokens as number | null,
      totalTokens: values.total_tokens as number | null,
      accounting,
      latestObservationId: String(latest.observation_id),
    };
  }

  dispatch(action: WorkspaceStateAction): TeamWorkspace {
    switch (action.action) {
      case "create-workspace": return this.createWorkspace(action);
      case "update-workspace": return this.updateWorkspace(action);
      case "attach-agent": return this.attachAgent(action);
      case "add-context": return this.addContext(action);
      case "ack-context": return this.ackContext(action);
      case "record-usage": return this.recordUsage(action);
    }
  }

  private createWorkspace(action: Extract<WorkspaceAction, { action: "create-workspace" }>): TeamWorkspace {
    const workspaceId = randomUUID();
    const timestamp = now();
    const title = cleanText(action.title, "title", { required: true, max: 200 });
    const objective = cleanText(action.objective, "objective", { required: true });
    const sharedRules = cleanText(action.sharedRules, "sharedRules");
    const hostBinding = action.hostBinding === undefined ? null : this.binding(action.hostBinding, "hostBinding");
    this.db.prepare(`
      INSERT INTO vt_workspaces(workspace_id, title, objective, shared_rules,
        host_binding_kind, host_binding_ref, leader_agent_id, status, version, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, NULL, 'active', 0, ?, ?)
    `).run(workspaceId, title, objective, sharedRules, hostBinding?.kind ?? null, hostBinding?.ref ?? null, timestamp, timestamp);
    return this.snapshot(workspaceId);
  }

  private binding(value: unknown, field: string): HostBinding {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${field} must be an object`);
    const input = value as Record<string, unknown>;
    return {
      kind: cleanText(input.kind, `${field}.kind`, { required: true, max: 100 }),
      ref: cleanText(input.ref, `${field}.ref`, { required: true, max: 500 }),
    };
  }

  private updateWorkspace(action: Extract<WorkspaceAction, { action: "update-workspace" }>): TeamWorkspace {
    const row = this.workspaceRow(action.workspaceId);
    const title = action.title === undefined ? String(row.title) : cleanText(action.title, "title", { required: true, max: 200 });
    const objective = action.objective === undefined ? String(row.objective) : cleanText(action.objective, "objective", { required: true });
    const sharedRules = action.sharedRules === undefined ? String(row.shared_rules) : cleanText(action.sharedRules, "sharedRules");
    const hostBinding = action.hostBinding === undefined
      ? parseHostBinding(row)
      : action.hostBinding === null ? null : this.binding(action.hostBinding, "hostBinding");
    const status = action.status ?? String(row.status) as WorkspaceStatus;
    if (!WORKSPACE_STATUSES.has(status)) throw new Error("invalid workspace status");
    const leaderAgentId = action.leaderAgentId === undefined
      ? row.leader_agent_id as string | null
      : nullableText(action.leaderAgentId, "leaderAgentId", 100);
    if (leaderAgentId !== null) this.agentRow(action.workspaceId, leaderAgentId);
    const timestamp = now();
    this.db.prepare(`
      UPDATE vt_workspaces SET title = ?, objective = ?, shared_rules = ?,
        host_binding_kind = ?, host_binding_ref = ?, leader_agent_id = ?, status = ?, version = version + 1,
        updated_at = ? WHERE workspace_id = ?
    `).run(title, objective, sharedRules, hostBinding?.kind ?? null, hostBinding?.ref ?? null, leaderAgentId, status, timestamp, action.workspaceId);
    return this.snapshot(action.workspaceId);
  }

  private attachAgent(action: Extract<WorkspaceAction, { action: "attach-agent" }>): TeamWorkspace {
    this.workspaceRow(action.workspaceId);
    const provider = cleanText(action.binding?.provider, "binding.provider", { required: true, max: 100 });
    const nativeSessionId = cleanText(action.binding?.nativeSessionId, "binding.nativeSessionId", { required: true, max: 500 });
    const source = action.attachSource ?? "manual";
    if ((source as string) === "created") throw new Error("created agents must come from a connected driver");
    return this.insertAgent({
      workspaceId: action.workspaceId,
      displayName: action.displayName,
      binding: { provider, nativeSessionId, nativeOpenRef: action.binding.nativeOpenRef ?? null },
      model: action.model,
      thinking: action.thinking,
      permissionMode: action.permissionMode,
      responsibility: action.responsibility,
      attachSource: source,
      asLeader: action.asLeader,
    });
  }

  /** Called only after a driver has actually returned a new native identity. */
  attachCreatedAgent(input: {
    workspaceId: string;
    displayName: string;
    provider: string;
    nativeSessionId: string;
    nativeOpenRef?: string;
    model?: string;
    thinking?: string;
    permissionMode?: string;
    responsibility?: string;
    asLeader?: boolean;
  }): TeamWorkspace {
    this.workspaceRow(input.workspaceId);
    return this.insertAgent({
      workspaceId: input.workspaceId,
      displayName: input.displayName,
      binding: {
        provider: cleanText(input.provider, "provider", { required: true, max: 100 }),
        nativeSessionId: cleanText(input.nativeSessionId, "nativeSessionId", { required: true, max: 500 }),
        nativeOpenRef: input.nativeOpenRef ?? null,
      },
      model: input.model,
      thinking: input.thinking,
      permissionMode: input.permissionMode,
      responsibility: input.responsibility,
      attachSource: "created",
      asLeader: input.asLeader,
    });
  }

  private insertAgent(input: {
    workspaceId: string;
    displayName: string;
    binding: AgentBinding;
    model?: string;
    thinking?: string;
    permissionMode?: string;
    responsibility?: string;
    attachSource: AgentAttachSource;
    asLeader?: boolean;
  }): TeamWorkspace {
    const provider = cleanText(input.binding.provider, "binding.provider", { required: true, max: 100 });
    const nativeSessionId = cleanText(input.binding.nativeSessionId, "binding.nativeSessionId", { required: true, max: 500 });
    const displayName = cleanText(input.displayName, "displayName", { required: true, max: 200 });
    const nativeOpenRef = nullableText(input.binding.nativeOpenRef, "binding.nativeOpenRef", 2_000);
    const model = nullableText(input.model, "model", 200);
    const thinking = nullableText(input.thinking, "thinking", 200);
    const permissionMode = nullableText(input.permissionMode, "permissionMode", 200);
    const responsibility = cleanText(input.responsibility, "responsibility");
    if (!ATTACH_SOURCES.has(input.attachSource)) throw new Error("invalid attach source");

    // Agent identity, bootstrap packet, and its workspace version are one
    // durable commit. Re-read the native owner under the write lock so a
    // second bridge cannot win between the preflight and INSERT.
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const existing = this.db.prepare(`
        SELECT agent_id, workspace_id FROM vt_agents WHERE provider = ? AND native_session_id = ?
      `).get(provider, nativeSessionId) as Row | undefined;
      if (existing && String(existing.workspace_id) !== input.workspaceId) {
        throw new Error(`native session is already attached to workspace ${String(existing.workspace_id)}`);
      }
      if (existing) {
        if (input.asLeader) {
          const timestamp = now();
          this.db.prepare("UPDATE vt_workspaces SET leader_agent_id = ?, version = version + 1, updated_at = ? WHERE workspace_id = ?")
            .run(String(existing.agent_id), timestamp, input.workspaceId);
        }
        this.db.exec("COMMIT");
        return this.snapshot(input.workspaceId);
      }

      const workspace = this.workspaceRow(input.workspaceId);
      const agentId = randomUUID();
      const timestamp = now();
      this.db.prepare(`
        INSERT INTO vt_agents(agent_id, workspace_id, display_name, provider,
          native_session_id, native_open_ref, model, thinking, permission_mode,
          responsibility, status, attach_source, context_version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 0, ?, ?)
      `).run(
        agentId,
        input.workspaceId,
        displayName,
        provider,
        nativeSessionId,
        nativeOpenRef,
        model,
        thinking,
        permissionMode,
        responsibility,
        input.attachSource,
        timestamp,
        timestamp,
      );

      // Every newly attached Agent receives exactly one pending, target-only
      // snapshot of the durable objective/rules. Delivery remains an
      // explicit action; later direct commands never call this path.
      const version = Number(workspace.version) + 1;
      const result = this.db.prepare(`
        INSERT INTO vt_context_updates(workspace_id, version, summary, source_ref, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
      `).run(
        input.workspaceId,
        version,
        bootstrapSummary(String(workspace.objective), String(workspace.shared_rules)),
        "visible-team:workspace-bootstrap",
        "visible-team",
        timestamp,
      );
      this.db.prepare("INSERT INTO vt_context_targets(update_id, agent_id, delivered_at) VALUES (?, ?, NULL)")
        .run(Number(result.lastInsertRowid), agentId);
      this.db.prepare(`
        UPDATE vt_workspaces SET leader_agent_id = ?, version = ?, updated_at = ? WHERE workspace_id = ?
      `).run(
        input.asLeader
          ? agentId
          : workspace.leader_agent_id === null || workspace.leader_agent_id === undefined
            ? null
            : String(workspace.leader_agent_id),
        version,
        timestamp,
        input.workspaceId,
      );
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
    return this.snapshot(input.workspaceId);
  }

  private addContext(action: Extract<WorkspaceAction, { action: "add-context" }>): TeamWorkspace {
    this.workspaceRow(action.workspaceId);
    if (!Array.isArray(action.targets)) throw new Error("targets must be an explicit array");
    const targets = [...new Set(action.targets.map(id => cleanText(id, "target", { required: true, max: 100 })))];
    if (targets.length === 0) throw new Error("at least one target agent is required");
    if (targets.some(id => id.toLowerCase() === "all")) throw new Error("targets=all is forbidden; list each Agent explicitly");
    for (const agentId of targets) this.agentRow(action.workspaceId, agentId);

    // The read and increment intentionally happen after BEGIN IMMEDIATE. A
    // stale pre-transaction version would race two concurrent packets.
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const row = this.workspaceRow(action.workspaceId);
      const version = Number(row.version) + 1;
      const timestamp = now();
      const result = this.db.prepare(`
        INSERT INTO vt_context_updates(workspace_id, version, summary, source_ref, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
      `).run(
        action.workspaceId,
        version,
        cleanText(action.summary, "summary", { required: true }),
        nullableText(action.sourceRef, "sourceRef", 2_000),
        cleanText(action.createdBy ?? "user", "createdBy", { required: true, max: 100 }),
        timestamp,
      );
      const updateId = Number(result.lastInsertRowid);
      const insertTarget = this.db.prepare("INSERT INTO vt_context_targets(update_id, agent_id, delivered_at) VALUES (?, ?, NULL)");
      for (const agentId of targets) insertTarget.run(updateId, agentId);
      this.db.prepare("UPDATE vt_workspaces SET version = ?, updated_at = ? WHERE workspace_id = ?")
        .run(version, timestamp, action.workspaceId);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
    return this.snapshot(action.workspaceId);
  }

  ackContext(action: Extract<WorkspaceAction, { action: "ack-context" }>): TeamWorkspace {
    this.workspaceRow(action.workspaceId);
    this.agentRow(action.workspaceId, action.agentId);
    if (!Number.isSafeInteger(action.throughVersion) || action.throughVersion < 0) throw new Error("throughVersion must be a non-negative integer");
    const timestamp = now();
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db.prepare(`
        UPDATE vt_context_targets SET delivered_at = ?
        WHERE agent_id = ? AND delivered_at IS NULL AND update_id IN (
          SELECT update_id FROM vt_context_updates WHERE workspace_id = ? AND version <= ?
        )
      `).run(timestamp, action.agentId, action.workspaceId, action.throughVersion);
      const maxRow = this.db.prepare(`
        SELECT MAX(u.version) AS version
        FROM vt_context_targets t
        JOIN vt_context_updates u ON u.update_id = t.update_id
        WHERE t.agent_id = ? AND u.workspace_id = ? AND t.delivered_at IS NOT NULL
      `).get(action.agentId, action.workspaceId) as Row;
      this.db.prepare(`
        UPDATE vt_agents SET context_version = COALESCE(?, 0), updated_at = ?
        WHERE workspace_id = ? AND agent_id = ?
      `).run(maxRow.version === null ? null : Number(maxRow.version), timestamp, action.workspaceId, action.agentId);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
    return this.snapshot(action.workspaceId);
  }

  private recordUsage(action: Extract<WorkspaceAction, { action: "record-usage" }>): TeamWorkspace {
    this.workspaceRow(action.workspaceId);
    this.agentRow(action.workspaceId, action.agentId);
    const observationId = cleanText(action.observationId, "observationId", { required: true, max: 200 });
    const source = cleanText(action.source, "source", { required: true, max: 200 });
    if (!USAGE_ACCOUNTING.has(action.accounting)) throw new Error("accounting must be delta or cumulative");
    const counts = {
      inputTokens: optionalCount(action.inputTokens, "inputTokens"),
      cachedInputTokens: optionalCount(action.cachedInputTokens, "cachedInputTokens"),
      outputTokens: optionalCount(action.outputTokens, "outputTokens"),
      reasoningOutputTokens: optionalCount(action.reasoningOutputTokens, "reasoningOutputTokens"),
      totalTokens: optionalCount(action.totalTokens, "totalTokens"),
    };
    if (Object.values(counts).every(value => value === null)) throw new Error("at least one native usage counter is required");

    const existing = this.db.prepare(`
      SELECT * FROM vt_usage
      WHERE workspace_id = ? AND agent_id = ? AND source = ? AND observation_id = ?
    `).get(action.workspaceId, action.agentId, source, observationId) as Row | undefined;
    if (existing) {
      const same = String(existing.agent_id) === action.agentId
        && String(existing.source) === source
        && String(existing.accounting) === action.accounting
        && [
          [existing.input_tokens, counts.inputTokens],
          [existing.cached_input_tokens, counts.cachedInputTokens],
          [existing.output_tokens, counts.outputTokens],
          [existing.reasoning_output_tokens, counts.reasoningOutputTokens],
          [existing.total_tokens, counts.totalTokens],
        ].every(([left, right]) => (left === null ? null : Number(left)) === right);
      if (!same) throw new Error(`observationId is already used with different native usage`);
      return this.snapshot(action.workspaceId);
    }
    const stream = this.db.prepare("SELECT accounting FROM vt_usage WHERE agent_id = ? AND source = ? LIMIT 1")
      .get(action.agentId, source) as Row | undefined;
    if (stream && String(stream.accounting) !== action.accounting) {
      throw new Error(`usage source ${source} cannot switch between delta and cumulative accounting`);
    }
    this.db.prepare(`
      INSERT INTO vt_usage(workspace_id, agent_id, observation_id, source, accounting,
        input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens, observed_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      action.workspaceId,
      action.agentId,
      observationId,
      source,
      action.accounting,
      counts.inputTokens,
      counts.cachedInputTokens,
      counts.outputTokens,
      counts.reasoningOutputTokens,
      counts.totalTokens,
      action.observedAt === undefined ? now() : cleanText(action.observedAt, "observedAt", { required: true, max: 100 }),
    );
    return this.snapshot(action.workspaceId);
  }
}
