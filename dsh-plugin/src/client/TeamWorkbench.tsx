import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type FormEvent } from "react";
import {
  Button,
  IconAgentPresetOutline16,
  IconCloseOutline16,
  IconPlusOutline16,
  IconRefreshOutline16,
  Input,
  Tooltip,
  useDismissOnOutsidePointer,
} from "@deepseek-ai/dsh-client-ui-primitives";
import type { TeamClient } from "./entry.js";
import type { TeamWorkspace } from "../shared/types.js";
import css, { dispose as disposeStyles } from "./TeamWorkbench.module.css";

/** Called by the plugin effect when DSH tears down the client fiber. */
export function disposeTeamWorkbenchStyles(): void {
  disposeStyles();
}

export interface TeamWorkbenchProps {
  /** Sidebar column state from the public `sidebar.footer.action` owner share. */
  wide: boolean;
  team: TeamClient;
}

interface CreateDraft {
  title: string;
  objective: string;
}

const EMPTY_DRAFT: CreateDraft = { title: "", objective: "" };

function statusLabel(status: TeamWorkspace["status"]): string {
  switch (status) {
    case "active": return "进行中";
    case "paused": return "已暂停";
    case "completed": return "已完成";
    case "cancelled": return "已取消";
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function workspaceSummary(workspace: TeamWorkspace): string {
  return `${workspace.agents.length} 位成员 · ${statusLabel(workspace.status)}`;
}

function updateAnchor(root: HTMLDivElement | null): CSSProperties | null {
  if (root === null || typeof window === "undefined") return null;
  const rect = root.getBoundingClientRect();
  return { left: rect.left, bottom: window.innerHeight - rect.top + 8 };
}

/**
 * Always-visible sidebar action and the compact workspace browser it opens.
 * Full editing and Agent controls remain in the session-scoped Team tab.
 */
export function TeamWorkbench({ wide, team }: TeamWorkbenchProps) {
  const state = useSyncExternalStore(team.subscribe, team.getSnapshot, team.getSnapshot);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<CSSProperties | null>(null);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<CreateDraft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // The footer lives inside the sidebar's clipped column. Keep the panel fixed
  // and recompute on viewport changes so the wider surface remains anchored.
  useLayoutEffect(() => {
    if (!open) {
      setAnchor(null);
      return;
    }
    const place = () => { setAnchor(updateAnchor(rootRef.current)); };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useDismissOnOutsidePointer(rootRef, open, setOpen);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    void team.refresh();
    return () => { document.removeEventListener("keydown", closeOnEscape); };
  }, [open, team]);

  const selected = useMemo(
    () => state.workspaces.find(workspace => workspace.workspaceId === selectedId) ?? state.workspaces[0],
    [selectedId, state.workspaces],
  );

  useEffect(() => {
    if (selectedId !== undefined && state.workspaces.some(workspace => workspace.workspaceId === selectedId)) return;
    setSelectedId(state.workspaces[0]?.workspaceId);
  }, [selectedId, state.workspaces]);

  const createWorkspace = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const title = draft.title.trim();
    const objective = draft.objective.trim();
    if (!title || !objective) {
      setError("请填写协作空间名称和目标。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await team.dispatch({ action: "create-workspace", title, objective });
      const created = result.workspaces[0];
      if (created !== undefined) setSelectedId(created.workspaceId);
      setDraft(EMPTY_DRAFT);
      setCreating(false);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const panel = open && anchor !== null ? (
    <section
      id="visible-team-workbench-panel"
      className={css.panel}
      style={anchor}
      data-visible-team-workbench
      aria-label="协作工作台"
    >
      <header className={css.header}>
        <div className={css.titleGroup}>
          <h2 className={css.title}>协作工作台</h2>
          <span className={css.subtitle}>快速查看空间；详细管理请打开 Team 标签页</span>
        </div>
        <Tooltip label="关闭协作工作台" side="bottom" delayMs={500}>
          <button type="button" className={css.closeButton} aria-label="关闭协作工作台" onClick={() => setOpen(false)}>
            <IconCloseOutline16 size={16} />
          </button>
        </Tooltip>
      </header>

      <div className={css.body}>
        {state.state === "loading" && state.workspaces.length === 0 ? <p className={css.status}>正在加载协作空间…</p> : null}
        {state.error !== null || error !== null ? <p className={css.error} role="alert">{error ?? state.error}</p> : null}

        {state.workspaces.length === 0 && state.state !== "loading" ? (
          <div className={css.empty} data-visible-team-empty>
            <span className={css.emptyIcon} aria-hidden="true"><IconAgentPresetOutline16 size={20} /></span>
            <h3 className={css.emptyTitle}>还没有协作空间</h3>
            <p className={css.emptyDescription}>先创建一个空间，再关联会话，最后选择 Leader。打开 Team 标签页即可继续完成后两步。</p>
            <Button
              variant="primary"
              size="sm"
              icon={<IconPlusOutline16 size={14} />}
              onClick={() => { setCreating(true); setError(null); }}
              disabled={busy}
            >
              创建协作空间
            </Button>
          </div>
        ) : null}

        {state.workspaces.length > 0 ? (
          <>
            <div className={css.toolbar}>
              <div className={css.titleGroup}>
                <h3 className={css.sectionTitle}>协作空间</h3>
                <span className={css.muted}>{state.workspaces.length} 个空间</span>
              </div>
              <div className={css.toolbar}>
                <Tooltip label="刷新协作空间" side="bottom" delayMs={500}>
                  <button type="button" className={css.refreshButton} aria-label="刷新协作空间" onClick={() => { void team.refresh(); }} disabled={busy}>
                    <IconRefreshOutline16 size={16} />
                  </button>
                </Tooltip>
                <Button variant="primary" size="sm" icon={<IconPlusOutline16 size={14} />} onClick={() => { setCreating(value => !value); setError(null); }} disabled={busy}>
                  新建
                </Button>
              </div>
            </div>
            <ul className={css.list} aria-label="协作空间列表">
              {state.workspaces.map(workspace => (
                <li key={workspace.workspaceId}>
                  <button
                    type="button"
                    className={css.workspaceItem}
                    data-active={workspace.workspaceId === selected?.workspaceId}
                    aria-pressed={workspace.workspaceId === selected?.workspaceId}
                    onClick={() => setSelectedId(workspace.workspaceId)}
                  >
                    <span className={css.workspaceIcon} aria-hidden="true"><IconAgentPresetOutline16 size={16} /></span>
                    <span className={css.workspaceCopy}>
                      <span className={css.workspaceName}>{workspace.title}</span>
                      <span className={css.workspaceObjective}>{workspace.objective}</span>
                    </span>
                    <span className={css.workspaceStats}>{workspaceSummary(workspace)}</span>
                  </button>
                </li>
              ))}
            </ul>
            {selected !== undefined ? (
              <div className={css.detail} data-visible-team-selection>
                <div className={css.detailHeading}>
                  <span className={css.detailTitle}>{selected.title}</span>
                  <span className={css.muted}>v{selected.version}</span>
                </div>
                <p className={css.detailObjective}>{selected.objective}</p>
                <div className={css.detailStats}>{workspaceSummary(selected)} · {selected.context.length} 个上下文包</div>
              </div>
            ) : null}
          </>
        ) : null}

        {creating ? (
          <form className={css.createSection} onSubmit={event => void createWorkspace(event)} data-visible-team-create>
            <div className={css.sectionHeader}>
              <h3 className={css.sectionTitle}>新建协作空间</h3>
              <Button variant="ghost" size="sm" onClick={() => { setCreating(false); setError(null); }} disabled={busy}>取消</Button>
            </div>
            <div className={css.createForm}>
              <label className={css.field}>
                空间名称
                <Input
                  className={css.input}
                  aria-label="空间名称"
                  placeholder="例如：发布前检查"
                  value={draft.title}
                  onChange={event => setDraft(current => ({ ...current, title: event.target.value }))}
                  autoFocus
                />
              </label>
              <label className={css.field}>
                协作目标
                <textarea
                  className={css.textarea}
                  aria-label="协作目标"
                  placeholder="这个空间要共同完成什么？"
                  value={draft.objective}
                  onChange={event => setDraft(current => ({ ...current, objective: event.target.value }))}
                />
              </label>
              <div className={css.formActions}>
                <Button variant="primary" size="sm" type="submit" icon={<IconPlusOutline16 size={14} />} disabled={busy || !draft.title.trim() || !draft.objective.trim()}>
                  {busy ? "创建中…" : "创建空间"}
                </Button>
              </div>
            </div>
          </form>
        ) : null}
      </div>
    </section>
  ) : null;

  return (
    <div ref={rootRef} className={wide ? css.layer : `${css.layer} ${css.rail}`}>
      {panel}
      <div className={css.footerButtons}>
        <Tooltip label="打开协作工作台" side="right" delayMs={500} disabled={wide}>
          <button
            type="button"
            className={wide ? css.trigger : `${css.trigger} ${css.triggerRail}`}
            aria-label="打开协作工作台"
            aria-controls="visible-team-workbench-panel"
            aria-expanded={open}
            data-visible-team-trigger
            onClick={() => setOpen(value => !value)}
          >
            <IconAgentPresetOutline16 size={wide ? 16 : 18} />
            {wide ? <><span className={css.triggerLabel}>协作工作台</span><span className={css.triggerCount}>{state.workspaces.length} 个空间</span></> : null}
          </button>
        </Tooltip>
      </div>
    </div>
  );
}
