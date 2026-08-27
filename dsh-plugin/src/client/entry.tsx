import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type FormEvent,
} from "react";
import {
  API_PATH,
  EVENTS_PATH,
  type TeamAgent,
  type TeamWorkspace,
  type WorkspaceAction,
  type WorkspaceCommandResult,
} from "../shared/types.js";

type Listener = () => void;
type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface EventSourceLike {
  addEventListener(type: string, listener: () => void): void;
  close(): void;
}

export interface TeamClientSnapshot {
  workspaces: TeamWorkspace[];
  state: "idle" | "loading" | "error";
  error: string | null;
}

export interface TeamClientOptions {
  apiPath?: string;
  eventsPath?: string;
  fetch?: FetchLike;
  eventSource?: () => EventSourceLike | undefined;
}

/** Small browser client for the same Host action contract used by agents. */
export class TeamClient {
  private state: TeamClientSnapshot = { workspaces: [], state: "idle", error: null };
  private readonly listeners = new Set<Listener>();
  private readonly apiPath: string;
  private readonly eventsPath: string;
  private readonly fetchImpl: FetchLike;
  private readonly eventSourceFactory: (() => EventSourceLike | undefined) | undefined;
  private eventSource: EventSourceLike | undefined;
  private started = false;
  private requestSeq = 0;

  constructor(options: TeamClientOptions = {}) {
    this.apiPath = options.apiPath ?? API_PATH;
    this.eventsPath = options.eventsPath ?? EVENTS_PATH;
    this.fetchImpl = options.fetch ?? ((input, init) => fetch(input, init));
    this.eventSourceFactory = options.eventSource ?? (() => {
      if (typeof EventSource === "undefined") return undefined;
      return new EventSource(this.eventsPath);
    });
  }

  getSnapshot = (): TeamClientSnapshot => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private publish(next: TeamClientSnapshot): void {
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  private mergeWorkspaces(next: readonly TeamWorkspace[], replace: boolean): void {
    const byId = new Map(replace ? [] : this.state.workspaces.map(workspace => [workspace.workspaceId, workspace] as const));
    for (const workspace of next) byId.set(workspace.workspaceId, workspace);
    this.publish({
      workspaces: [...byId.values()].sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
      state: "idle",
      error: null,
    });
  }

  async refresh(workspaceId?: string): Promise<void> {
    const request = ++this.requestSeq;
    this.publish({ ...this.state, state: "loading", error: null });
    const url = workspaceId === undefined
      ? this.apiPath
      : `${this.apiPath}?workspace=${encodeURIComponent(workspaceId)}`;
    try {
      const response = await this.fetchImpl(url, { headers: { accept: "application/json" } });
      const payload = await response.json() as { workspaces?: TeamWorkspace[]; error?: string };
      if (!response.ok) throw new Error(payload.error || `Host request failed (${response.status})`);
      if (request !== this.requestSeq) return;
      this.mergeWorkspaces(payload.workspaces ?? [], workspaceId === undefined);
    } catch (error) {
      if (request !== this.requestSeq) return;
      this.publish({ ...this.state, state: "error", error: error instanceof Error ? error.message : String(error) });
    }
  }

  async dispatch(action: WorkspaceAction): Promise<WorkspaceCommandResult> {
    const response = await this.fetchImpl(this.apiPath, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(action),
    });
    const payload = await response.json() as WorkspaceCommandResult & {
      error?: string;
      code?: string;
      capability?: string;
      message?: string;
    };
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || `Host request failed (${response.status})`) as TeamApiError;
      error.code = payload.code;
      error.capability = payload.capability;
      throw error;
    }
    this.mergeWorkspaces(payload.workspaces ?? [], false);
    return payload;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    void this.refresh();
    try {
      this.eventSource = this.eventSourceFactory?.();
      this.eventSource?.addEventListener("change", () => { void this.refresh(); });
    } catch {
      // The state API remains usable when the optional SSE channel is absent.
      this.eventSource = undefined;
    }
  }

  dispose(): void {
    this.started = false;
    this.eventSource?.close();
    this.eventSource = undefined;
    this.listeners.clear();
  }
}

interface TeamApiError extends Error {
  code?: string;
  capability?: string;
}

const css: Record<string, CSSProperties> = {
  root: { display: "flex", flexDirection: "column", gap: 16, padding: 20, maxWidth: 1100, margin: "0 auto", color: "var(--dsh-text-primary, #e7e9ee)" },
  columns: { display: "grid", gridTemplateColumns: "minmax(190px, 0.8fr) minmax(0, 2fr)", gap: 16, alignItems: "start" },
  panel: { border: "1px solid var(--dsh-border, #30343b)", borderRadius: 10, padding: 14, background: "var(--dsh-surface, #17191e)" },
  muted: { color: "var(--dsh-text-secondary, #9ca3af)", fontSize: 12 },
  label: { display: "flex", flexDirection: "column", gap: 5, fontSize: 12, color: "var(--dsh-text-secondary, #b1b5bd)" },
  input: { width: "100%", boxSizing: "border-box", border: "1px solid var(--dsh-border, #3b4048)", borderRadius: 6, padding: "7px 8px", color: "inherit", background: "var(--dsh-input, #111318)" },
  textarea: { width: "100%", minHeight: 72, boxSizing: "border-box", resize: "vertical", border: "1px solid var(--dsh-border, #3b4048)", borderRadius: 6, padding: "7px 8px", color: "inherit", background: "var(--dsh-input, #111318)" },
  button: { border: "1px solid var(--dsh-border, #454b55)", borderRadius: 6, padding: "6px 9px", color: "inherit", background: "var(--dsh-control, #252a32)", cursor: "pointer" },
  primary: { borderColor: "var(--dsh-accent, #6d8cff)", background: "var(--dsh-accent, #405fc7)" },
  row: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  agent: { display: "flex", flexDirection: "column", gap: 9, borderTop: "1px solid var(--dsh-border, #30343b)", padding: "12px 0" },
};

function buttonStyle(primary = false): CSSProperties {
  return { ...css.button, ...(primary ? css.primary : {}) };
}

function formatUsage(agent: TeamAgent): string {
  const total = agent.usage.totalTokens;
  if (total === null) return "Token: unavailable (未收到原生计数)";
  const mode = agent.usage.accounting === null ? "mixed" : agent.usage.accounting;
  return `Token: ${total.toLocaleString()} (${mode})`;
}

function errorText(error: unknown): string {
  const typed = error as TeamApiError;
  if (typed?.code === "capability-unavailable") {
    return `${typed.capability ?? "capability"} unavailable: ${typed.message}`;
  }
  return error instanceof Error ? error.message : String(error);
}

interface AgentRowProps {
  team: TeamClient;
  workspace: TeamWorkspace;
  agent: TeamAgent;
  openSession?: (sessionId: string) => void;
  setError: (value: string | null) => void;
  busy: string | null;
}

function AgentRow({ team, workspace, agent, openSession, setError, busy }: AgentRowProps) {
  const [command, setCommand] = useState("");
  const leader = workspace.leaderAgentId === agent.agentId;
  const send = async (): Promise<void> => {
    if (!command.trim()) return;
    setError(null);
    try {
      await team.dispatch({ action: "send-agent", workspaceId: workspace.workspaceId, agentId: agent.agentId, content: command });
      setCommand("");
    } catch (error) {
      setError(errorText(error));
    }
  };
  const deliver = async (): Promise<void> => {
    setError(null);
    try {
      await team.dispatch({ action: "deliver-context", workspaceId: workspace.workspaceId, agentId: agent.agentId });
    } catch (error) {
      setError(errorText(error));
    }
  };
  const makeLeader = async (): Promise<void> => {
    setError(null);
    try {
      await team.dispatch({ action: "update-workspace", workspaceId: workspace.workspaceId, leaderAgentId: agent.agentId });
    } catch (error) {
      setError(errorText(error));
    }
  };
  return (
    <div style={css.agent}>
      <div style={css.row}>
        <strong>{agent.displayName}</strong>
        {leader ? <span style={css.muted}>Leader</span> : null}
        <span style={css.muted}>{agent.binding.provider} · {agent.binding.nativeSessionId}</span>
        <span style={css.muted}>{agent.status}</span>
      </div>
      <div style={css.muted}>{agent.responsibility || "未设置职责"} · {formatUsage(agent)}</div>
      <div style={css.row}>
        {!leader ? <button type="button" style={buttonStyle()} onClick={() => void makeLeader()} disabled={busy !== null}>设为 Leader</button> : null}
        {agent.binding.provider === "dsh" && openSession ? (
          <button type="button" style={buttonStyle()} onClick={() => openSession(agent.binding.nativeSessionId)}>打开 DSH Session</button>
        ) : null}
        <button type="button" style={buttonStyle()} onClick={() => void deliver()} disabled={busy !== null || agent.pendingContext === 0}>
          发送待同步 ({agent.pendingContext})
        </button>
      </div>
      <div style={css.row}>
        <input
          aria-label={`指挥 ${agent.displayName}`}
          style={{ ...css.input, flex: "1 1 280px" }}
          value={command}
          onChange={event => setCommand(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send(); }}
          placeholder={agent.binding.provider === "dsh" ? "直接指挥此 Agent（Cmd/Ctrl+Enter 发送）" : "外部 Agent 尚无 driver"}
        />
        <button type="button" style={buttonStyle(true)} onClick={() => void send()} disabled={!command.trim() || busy !== null}>发送</button>
      </div>
    </div>
  );
}

interface TeamViewProps {
  team: TeamClient;
  currentSessionId?: string;
  openSession?: (sessionId: string) => void;
}

function TeamView({ team, currentSessionId, openSession }: TeamViewProps) {
  const state = useSyncExternalStore(team.subscribe, team.getSnapshot, team.getSnapshot);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({ title: "", objective: "", sharedRules: "" });
  const [newWorkspace, setNewWorkspace] = useState({ title: "", objective: "", sharedRules: "" });
  const [currentName, setCurrentName] = useState("当前 DSH Session");
  const [external, setExternal] = useState({ provider: "codex", displayName: "", nativeSessionId: "", nativeOpenRef: "", responsibility: "" });
  const [context, setContext] = useState({ summary: "", sourceRef: "", targets: [] as string[] });
  const selected = useMemo(
    () => state.workspaces.find(workspace => workspace.workspaceId === selectedId) ?? state.workspaces[0],
    [selectedId, state.workspaces],
  );

  useEffect(() => {
    if (selectedId !== undefined && state.workspaces.some(workspace => workspace.workspaceId === selectedId)) return;
    setSelectedId(state.workspaces[0]?.workspaceId);
  }, [selectedId, state.workspaces]);

  useEffect(() => {
    if (!selected) {
      setDraft({ title: "", objective: "", sharedRules: "" });
      setContext({ summary: "", sourceRef: "", targets: [] });
      return;
    }
    setDraft({ title: selected.title, objective: selected.objective, sharedRules: selected.sharedRules });
    setContext(current => ({ ...current, targets: current.targets.filter(id => selected.agents.some(agent => agent.agentId === id)) }));
  }, [selected?.workspaceId, selected?.version]);

  const run = async (key: string, action: WorkspaceAction, after?: (result: WorkspaceCommandResult) => void): Promise<void> => {
    setBusy(key);
    setError(null);
    try {
      const result = await team.dispatch(action);
      after?.(result);
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setBusy(null);
    }
  };

  const create = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    await run("create-workspace", {
      action: "create-workspace",
      title: newWorkspace.title,
      objective: newWorkspace.objective,
      sharedRules: newWorkspace.sharedRules,
    }, result => {
      const workspace = result.workspaces[0];
      if (workspace) {
        setSelectedId(workspace.workspaceId);
        setNewWorkspace({ title: "", objective: "", sharedRules: "" });
      }
    });
  };

  if (selected === undefined) {
    return (
      <div style={css.root}>
        <header>
          <h2 style={{ margin: 0 }}>Visible Team</h2>
          <p style={css.muted}>协作空间独立于 DSH Workspace；规则、目标和 Agent 挂接会持久保存。</p>
        </header>
        <form style={css.panel} onSubmit={event => void create(event)}>
          <h3 style={{ marginTop: 0 }}>创建协作空间</h3>
          <div style={{ display: "grid", gap: 10 }}>
            <label style={css.label}>名称<input style={css.input} value={newWorkspace.title} onChange={event => setNewWorkspace({ ...newWorkspace, title: event.target.value })} /></label>
            <label style={css.label}>目标<textarea style={css.textarea} value={newWorkspace.objective} onChange={event => setNewWorkspace({ ...newWorkspace, objective: event.target.value })} /></label>
            <label style={css.label}>共享规则<textarea style={css.textarea} value={newWorkspace.sharedRules} onChange={event => setNewWorkspace({ ...newWorkspace, sharedRules: event.target.value })} /></label>
            <button type="submit" style={buttonStyle(true)} disabled={busy !== null}>创建协作空间</button>
          </div>
        </form>
        {state.error ? <p role="alert">{state.error}</p> : null}
        {error ? <p role="alert">{error}</p> : null}
      </div>
    );
  }

  const saveWorkspace = (event: FormEvent): void => {
    event.preventDefault();
    void run("update-workspace", {
      action: "update-workspace",
      workspaceId: selected.workspaceId,
      title: draft.title,
      objective: draft.objective,
      sharedRules: draft.sharedRules,
    });
  };

  const attachCurrent = (): void => {
    if (!currentSessionId) return;
    void run("attach-current", {
      action: "attach-agent",
      workspaceId: selected.workspaceId,
      displayName: currentName || "当前 DSH Session",
      binding: { provider: "dsh", nativeSessionId: currentSessionId },
      responsibility: "当前 DSH Session",
      attachSource: "manual",
    });
  };

  const attachExternal = (event: FormEvent): void => {
    event.preventDefault();
    void run("attach-external", {
      action: "attach-agent",
      workspaceId: selected.workspaceId,
      displayName: external.displayName,
      binding: {
        provider: external.provider,
        nativeSessionId: external.nativeSessionId,
        nativeOpenRef: external.nativeOpenRef || undefined,
      },
      responsibility: external.responsibility,
      attachSource: "manual",
    }, () => setExternal({ ...external, displayName: "", nativeSessionId: "", nativeOpenRef: "", responsibility: "" }));
  };

  const addContext = (event: FormEvent): void => {
    event.preventDefault();
    if (context.targets.length === 0) {
      setError("请选择至少一个明确的目标 Agent；不会隐式广播。");
      return;
    }
    void run("add-context", {
      action: "add-context",
      workspaceId: selected.workspaceId,
      summary: context.summary,
      sourceRef: context.sourceRef || undefined,
      targets: context.targets,
    }, () => setContext({ summary: "", sourceRef: "", targets: [] }));
  };

  return (
    <div style={css.root}>
      <header>
        <div style={css.row}><h2 style={{ margin: 0 }}>Visible Team</h2><span style={css.muted}>Team tab</span></div>
        <p style={css.muted}>协作空间是目标边界，不等同于 DSH Workspace。每个 Agent 都可直接指挥；Leader 只是同一 action contract 的可变引用。</p>
      </header>
      {(state.error || error) ? <p role="alert" style={{ color: "#ff9c9c" }}>{state.error || error}</p> : null}
      <div style={css.columns}>
        <aside style={css.panel}>
          <h3 style={{ marginTop: 0 }}>协作空间</h3>
          <div style={{ display: "grid", gap: 6 }}>
            {state.workspaces.map(workspace => (
              <button
                key={workspace.workspaceId}
                type="button"
                style={{ ...buttonStyle(workspace.workspaceId === selected.workspaceId), textAlign: "left" }}
                onClick={() => setSelectedId(workspace.workspaceId)}
              >
                <strong>{workspace.title}</strong><br /><span style={css.muted}>{workspace.agents.length} Agents · v{workspace.version}</span>
              </button>
            ))}
          </div>
          <form onSubmit={event => void create(event)} style={{ display: "grid", gap: 8, marginTop: 16 }}>
            <h4 style={{ margin: 0 }}>新建空间</h4>
            <input aria-label="新空间名称" style={css.input} placeholder="名称" value={newWorkspace.title} onChange={event => setNewWorkspace({ ...newWorkspace, title: event.target.value })} />
            <textarea aria-label="新空间目标" style={css.textarea} placeholder="目标" value={newWorkspace.objective} onChange={event => setNewWorkspace({ ...newWorkspace, objective: event.target.value })} />
            <button type="submit" style={buttonStyle(true)} disabled={busy !== null}>创建</button>
          </form>
        </aside>

        <main style={{ display: "grid", gap: 14 }}>
          <form style={css.panel} onSubmit={saveWorkspace}>
            <div style={css.row}><h3 style={{ margin: 0, flex: 1 }}>空间规则与目标</h3><span style={css.muted}>状态：{selected.status}</span></div>
            <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
              <label style={css.label}>名称<input style={css.input} value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} /></label>
              <label style={css.label}>目标<textarea style={css.textarea} value={draft.objective} onChange={event => setDraft({ ...draft, objective: event.target.value })} /></label>
              <label style={css.label}>共享规则<textarea style={css.textarea} value={draft.sharedRules} onChange={event => setDraft({ ...draft, sharedRules: event.target.value })} /></label>
              <button type="submit" style={buttonStyle(true)} disabled={busy !== null}>保存空间</button>
            </div>
          </form>

          <section style={css.panel}>
            <h3 style={{ marginTop: 0 }}>挂接 Agent</h3>
            <div style={{ ...css.panel, marginBottom: 12 }}>
              <div style={css.row}><strong>挂接当前 DSH Session</strong><span style={css.muted}>{currentSessionId || "当前没有可挂接的 Session"}</span></div>
              <div style={{ ...css.row, marginTop: 8 }}>
                <input style={{ ...css.input, flex: "1 1 220px" }} value={currentName} onChange={event => setCurrentName(event.target.value)} placeholder="显示名称" />
                <button type="button" style={buttonStyle(true)} onClick={attachCurrent} disabled={!currentSessionId || busy !== null}>挂接现有 Session</button>
              </div>
            </div>
            <form onSubmit={attachExternal} style={{ display: "grid", gap: 9 }}>
              <strong>手动挂接外部 Agent（现有 native session/task）</strong>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 }}>
                <label style={css.label}>Provider<input style={css.input} value={external.provider} onChange={event => setExternal({ ...external, provider: event.target.value })} /></label>
                <label style={css.label}>显示名称<input style={css.input} value={external.displayName} onChange={event => setExternal({ ...external, displayName: event.target.value })} /></label>
                <label style={css.label}>native session/task ID<input style={css.input} value={external.nativeSessionId} onChange={event => setExternal({ ...external, nativeSessionId: event.target.value })} /></label>
                <label style={css.label}>打开引用（可选）<input style={css.input} value={external.nativeOpenRef} onChange={event => setExternal({ ...external, nativeOpenRef: event.target.value })} /></label>
              </div>
              <label style={css.label}>职责（可选）<input style={css.input} value={external.responsibility} onChange={event => setExternal({ ...external, responsibility: event.target.value })} /></label>
              <div style={css.row}><button type="submit" style={buttonStyle(true)} disabled={busy !== null}>挂接现有外部 Agent</button><span style={css.muted}>创建新 Agent 需要 provider driver；当前未连接时会明确返回 capability unavailable。</span></div>
            </form>
          </section>

          <section style={css.panel}>
            <h3 style={{ marginTop: 0 }}>Agent 与统一指挥</h3>
            {selected.agents.length === 0 ? <p style={css.muted}>尚未挂接 Agent。</p> : selected.agents.map(agent => (
              <AgentRow key={agent.agentId} team={team} workspace={selected} agent={agent} openSession={openSession} setError={setError} busy={busy} />
            ))}
          </section>

          <form style={css.panel} onSubmit={addContext}>
            <h3 style={{ marginTop: 0 }}>定向上下文增量</h3>
            <p style={css.muted}>必须逐一勾选目标；保存不会隐式广播。点击某个 Agent 的“发送待同步”才会通过该 Agent driver 发送，并在成功后确认版本。</p>
            <div style={{ display: "grid", gap: 9 }}>
              <label style={css.label}>上下文摘要<textarea style={css.textarea} value={context.summary} onChange={event => setContext({ ...context, summary: event.target.value })} /></label>
              <label style={css.label}>来源引用（可选）<input style={css.input} value={context.sourceRef} onChange={event => setContext({ ...context, sourceRef: event.target.value })} /></label>
              <div style={{ ...css.row, alignItems: "flex-start" }}>
                <span style={css.label}>明确目标：</span>
                {selected.agents.map(agent => (
                  <label key={agent.agentId} style={{ ...css.row, fontSize: 12 }}>
                    <input
                      type="checkbox"
                      checked={context.targets.includes(agent.agentId)}
                      onChange={event => setContext({ ...context, targets: event.target.checked ? [...context.targets, agent.agentId] : context.targets.filter(id => id !== agent.agentId) })}
                    />
                    {agent.displayName}
                  </label>
                ))}
              </div>
              <button type="submit" style={buttonStyle(true)} disabled={busy !== null || !context.summary.trim()}>保存定向增量</button>
            </div>
            {selected.context.length > 0 ? (
              <div style={{ marginTop: 14 }}>
                <strong>最近上下文包</strong>
                {selected.context.map(packet => <div key={packet.updateId} style={{ ...css.muted, borderTop: "1px solid var(--dsh-border, #30343b)", padding: "7px 0" }}>v{packet.version} → {packet.targets.length} 个明确目标 · {packet.summary}</div>)}
              </div>
            ) : null}
          </form>
        </main>
      </div>
    </div>
  );
}

interface ClientContext {
  slots: {
    inject(name: string, factory: () => unknown): void;
    register(options: Record<string, unknown>, component: unknown): unknown;
  };
  /** Public DSH session service declared by the plugin's inject face. */
  sessions: { open(sessionId: string): void };
  effect(factory: () => (() => void) | void, name?: string): void;
}

export const inject = ["slots", "sessions"];

export function apply(ctx: ClientContext): void {
  const team = new TeamClient();
  const sessions = ctx.sessions;
  ctx.effect(() => {
    team.start();
    return () => team.dispose();
  }, "visible-team: client state and event stream");
  ctx.slots.inject("conversation.view", () => ctx.slots.register({
    name: "conversation.view",
    id: "visible-team",
    order: 20,
    label: "Team",
    inject: (sessionId: string) => ({
      team,
      currentSessionId: sessionId,
      openSession: sessions?.open,
    }),
  }, TeamView));
}

export { TeamView };
