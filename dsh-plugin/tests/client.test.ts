import { afterEach, describe, expect, it, vi } from "vitest";
import { apply, inject, TeamWorkbench } from "../src/client/entry.js";

// The contract test does not render the browser surface. Keep the test runner
// on Node's CSS-free path while the production bundle still consumes DSH's
// official primitives through the public module-loader external.
vi.mock("@deepseek-ai/dsh-client-ui-primitives", () => {
  const Empty = () => null;
  return {
    Button: Empty,
    IconAgentPresetOutline16: Empty,
    IconCloseOutline16: Empty,
    IconPlusOutline16: Empty,
    IconRefreshOutline16: Empty,
    Input: Empty,
    Tooltip: Empty,
    useDismissOnOutsidePointer: () => {},
  };
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Visible Team Client injection contract", () => {
  it("declares public slots/sessions services and registers both the Team tab and sidebar entry", () => {
    expect(inject).toEqual(["slots", "sessions"]);

    const registered: { options: Record<string, unknown>; component: unknown }[] = [];
    const injectedSlots: string[] = [];
    const effects: (() => (() => void) | void)[] = [];
    const opened: string[] = [];
    const sessions = { open: (sessionId: string) => { opened.push(sessionId); } };
    const ctx = {
      slots: {
        inject(name: string, factory: () => unknown) {
          injectedSlots.push(name);
          factory();
        },
        register(options: Record<string, unknown>, component: unknown) {
          registered.push({ options, component });
          return { options, component };
        },
      },
      sessions,
      effect(factory: () => (() => void) | void) {
        effects.push(factory);
      },
    };

    apply(ctx);

    expect(injectedSlots).toEqual(["conversation.view", "sidebar.footer.action"]);
    expect(registered).toHaveLength(2);
    expect(registered[0]?.options).toMatchObject({
      name: "conversation.view",
      id: "visible-team",
      order: 20,
      label: "Team",
    });
    const perSession = (registered[0]?.options.inject as (sessionId: string) => {
      currentSessionId: string;
      openSession?: (sessionId: string) => void;
    })("session-1");
    expect(perSession.currentSessionId).toBe("session-1");
    perSession.openSession?.("session-2");
    expect(opened).toEqual(["session-2"]);
    expect(registered[1]?.options).toMatchObject({
      name: "sidebar.footer.action",
      id: "visible-team-workbench",
      order: 20,
      label: "协作工作台",
    });
    expect(registered[1]?.component).toBe(TeamWorkbench);
    const footerProps = (registered[1]?.options.inject as () => { team: unknown })();
    expect(footerProps.team).toBeDefined();
    expect(effects).toHaveLength(1);
  });
});
