import { afterEach, describe, expect, it, vi } from "vitest";
import { apply, inject } from "../src/client/entry.js";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Visible Team Client injection contract", () => {
  it("declares public slots/sessions services and registers the Team tab through conversation.view", () => {
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

    expect(injectedSlots).toEqual(["conversation.view"]);
    expect(registered).toHaveLength(1);
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
    expect(effects).toHaveLength(1);
  });
});
