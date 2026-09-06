import { act as reactAct } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "./test-fixture.json";
import App, { pages } from "./App";
import { ConfirmDialog } from "./components/ui";
import {
  client,
  DaemonError,
  request,
  type Config,
  type FullSnapshot,
  type Live,
} from "./client";
import { mutate, useConfig } from "./stores/configStore";
import { checkStale, useConnection } from "./stores/connectionStore";
import { useLive } from "./stores/liveStore";
import { connect } from "./stores/connect";
import {
  deletePoint,
  insertPoint,
  movePoint,
  validateCurve,
  interpolate,
} from "./curveMath";
import { toDisplay, temperature } from "./temperature";
import { actionFor, triggerFor } from "./pages/Automation";
vi.mock("uplot", () => ({
  default: class {
    setSize() {}
    destroy() {}
  },
}));

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement;
const config = () =>
  structuredClone(fixture.snapshot.config) as unknown as Config;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  }));
  window.__fanControl = {
    call: vi.fn(async (method: string) => {
      if (!(method in fixture))
        throw new DaemonError(`Unsupported ${method}`, "UNSUPPORTED");
      return structuredClone(fixture[method as keyof typeof fixture]);
    }),
    event: vi.fn(),
  };
  useConfig.setState({ config: null, snapshot: null, busy: false });
  useLive.setState({ live: null, history: [] });
  useConnection.setState({
    status: "reconnecting",
    capabilities: null,
    lastLive: 0,
    error: null,
  });
  container = document.createElement("div");
  document.body.append(container);
});
afterEach(async () => {
  if (root) await reactAct(async () => root?.unmount());
  root = null;
  container.remove();
  vi.restoreAllMocks();
  vi.useRealTimers();
});
async function flush() {
  await reactAct(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("workspace routes", () => {
  for (const page of pages)
    it(`initializes ${page} through the typed bridge`, async () => {
      window.location.hash = `/${page.toLowerCase()}`;
      root = createRoot(container);
      await reactAct(async () => root!.render(<App />));
      await flush();
      expect(container.querySelector("h1")?.textContent).toBe(page);
      expect(container.textContent).not.toContain(
        "Page implementation in progress",
      );
    });
  it("explains missing automation capability", async () => {
    window.__fanControl!.call = vi.fn(async (method) =>
      method === "capabilities"
        ? { ...fixture.capabilities, features: [] }
        : structuredClone(fixture[method as keyof typeof fixture]),
    );
    window.location.hash = "/automation";
    root = createRoot(container);
    await reactAct(async () => root!.render(<App />));
    await flush();
    expect(container.textContent).toContain(
      "This daemon does not support Automation",
    );
  });
});

describe("acknowledged configuration", () => {
  beforeEach(() => {
    useConfig.setState({
      config: config(),
      snapshot: fixture.snapshot as unknown as FullSnapshot,
    });
    useConnection.setState({ status: "connected" });
  });
  it("waits for acceptance and uses the daemon normalization", async () => {
    let resolve!: (value: unknown) => void;
    window.__fanControl!.call = vi.fn(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const old = useConfig.getState().config!;
    const pending = mutate("config", { max_duty: 79 });
    expect(useConfig.getState().config!.safety.global_max_duty).toBe(
      old.safety.global_max_duty,
    );
    expect(window.__fanControl!.call).toHaveBeenCalledWith("config", {
      max_duty: 79,
      expected_revision: old.revision,
    });
    const accepted = config();
    accepted.revision++;
    accepted.safety.global_max_duty = 80;
    resolve({ ok: true, config_revision: accepted.revision, config: accepted });
    await pending;
    expect(useConfig.getState().config!.safety.global_max_duty).toBe(80);
  });
  it("refreshes after revision conflict without retrying the mutation", async () => {
    window.__fanControl!.call = vi.fn(async (method) => {
      if (method === "config")
        throw new DaemonError("Changed elsewhere", "REVISION_CONFLICT");
      return { ...fixture.snapshot, config: { ...config(), revision: 99 } };
    });
    await expect(mutate("config", { max_duty: 80 })).rejects.toThrow(
      "Changed elsewhere",
    );
    expect(useConfig.getState().config!.revision).toBe(99);
    expect(window.__fanControl!.call).toHaveBeenCalledTimes(2);
  });
  it("preserves stable error codes", async () => {
    window.__fanControl!.call = vi
      .fn()
      .mockRejectedValue(
        Object.assign(new Error("Missing curve"), { code: "NOT_FOUND" }),
      );
    await expect(request("curves.assign")).rejects.toMatchObject({
      code: "NOT_FOUND",
    });
  });
});

describe("connection lifecycle", () => {
  it("marks frozen telemetry stale without zeroing values", () => {
    useLive.setState({ live: fixture.live as unknown as Live });
    useConnection.setState({ status: "connected", lastLive: 100 });
    checkStale(5200);
    expect(useConnection.getState().status).toBe("stale");
    expect(useLive.getState().live!.control_temp).toBe(
      fixture.live.control_temp,
    );
  });
  it("recovers automatically after initial connection failure", async () => {
    vi.useFakeTimers();
    let failed = true;
    window.__fanControl!.call = vi.fn(async (method) => {
      if (failed) throw new Error("Unavailable");
      return structuredClone(fixture[method as keyof typeof fixture]);
    });
    const stop = connect();
    await vi.advanceTimersByTimeAsync(1);
    expect(useConnection.getState().status).toBe("reconnecting");
    failed = false;
    await vi.advanceTimersByTimeAsync(2000);
    expect(useConnection.getState().status).toBe("connected");
    stop();
  });
  it("rejects incompatible protocol versions", async () => {
    vi.spyOn(client, "capabilities").mockResolvedValue({
      ...fixture.capabilities,
      protocol_version: 1,
    });
    const stop = connect();
    await flush();
    expect(useConnection.getState().status).toBe("unsupported-version");
    stop();
  });
  it("identifies a legacy daemon and reconnects after its upgrade", async () => {
    vi.useFakeTimers();
    const capabilities = vi.spyOn(client, "capabilities").mockRejectedValue(
      new Error("unknown method 'capabilities'"),
    );
    const stop = connect();
    await vi.advanceTimersByTimeAsync(1);
    expect(useConnection.getState().status).toBe("unsupported-version");
    expect(useConnection.getState().error).toContain("Update and restart fan-daemon");
    capabilities.mockResolvedValue(fixture.capabilities);
    await vi.advanceTimersByTimeAsync(2000);
    expect(useConnection.getState().status).toBe("connected");
    stop();
  });
});

it("curve preview is pure and cannot cross neighboring points", () => {
  const points: [number, number][] = [
    [30, 20],
    [60, 50],
    [90, 100],
  ];
  const moved = movePoint(points, 1, 100, 75);
  expect(moved[1]).toEqual([89, 75]);
  expect(points[1]).toEqual([60, 50]);
  const inserted = insertPoint(points, 45);
  expect(inserted[1]).toEqual([45, 35]);
  expect(interpolate(45, points)).toBe(35);
  expect(deletePoint(points.slice(0, 2), 0)).toHaveLength(2);
  expect(
    validateCurve([
      [30, 20],
      [30, 40],
    ]),
  ).not.toBeNull();
});
it("Fahrenheit conversion affects presentation only", () => {
  expect(toDisplay(100, "f")).toBe(212);
  expect(temperature(null, "f")).toBe("Unavailable");
  expect(temperature(0, "c")).toBe("0.0°C");
});
it("confirmation controls do not submit their enclosing settings form", async () => {
  const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
  const confirm = vi.fn();
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
  root = createRoot(container);
  await reactAct(async () => {
    root!.render(
      <form onSubmit={submit}>
        <ConfirmDialog
          label="Return to EC Auto"
          title="Confirm"
          onConfirm={confirm}
        >
          Return control to firmware.
        </ConfirmDialog>
      </form>,
    );
  });
  await reactAct(async () => container.querySelector("button")!.click());
  expect(container.querySelector("dialog")).not.toBeNull();
  expect(submit).not.toHaveBeenCalled();
  await reactAct(async () =>
    container.querySelector<HTMLButtonElement>("dialog .primary")!.click(),
  );
  expect(confirm).toHaveBeenCalledOnce();
  expect(submit).not.toHaveBeenCalled();
});
it("automation transformations remove fields from a prior trigger or action", () => {
  expect(triggerFor("on_startup")).toEqual({ type: "on_startup" });
  expect(actionFor("set_duty")).toEqual({
    type: "set_duty",
    fan: "fan1",
    pct: 60,
  });
});
