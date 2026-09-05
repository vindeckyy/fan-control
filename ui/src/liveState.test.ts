import { describe, expect, it } from "vitest";
import { applyLive, curvesEqual, windowedHistory } from "./liveState";
import type { HistoryPoint, Snapshot } from "./types";

const snap = {
  fan1: 40,
  fan2: 33,
  targets: { "1": 33, "2": 33 },
  custom_curve: [[50, 20], [80, 100]],
  named_curves: { quiet: [[40, 10], [90, 80]] },
} as unknown as Snapshot;

describe("applyLive", () => {
  it("updates telemetry without dropping config-only fields", () => {
    const next = applyLive(snap, {
      fan1: 33,
      fan2: 33,
      fan3: 0,
      rpm1: 2100,
      rpm2: 1800,
      rpm3: null,
      temps: [],
      primary_temp: 56,
      gpu_temp: 46,
      control_temp: 56,
      targets: { "1": 33, "2": 33 },
      mode: "manual",
      profile: "balanced",
      updated: 1,
      critical_active: false,
      fault_missing: 0,
      linked: true,
    });
    expect(next.fan1).toBe(33);
    expect(next.custom_curve).toEqual([[50, 20], [80, 100]]);
    expect(next.named_curves.quiet).toBeDefined();
  });
});

describe("windowedHistory", () => {
  it("keeps only points inside the selected window", () => {
    const history: HistoryPoint[] = [
      { time: 100, temp: 50, gpu_temp: 40, control_temp: 50, fan1: 20, fan2: 20, rpm1: null, rpm2: null },
      { time: 1000, temp: 55, gpu_temp: 44, control_temp: 55, fan1: 33, fan2: 33, rpm1: null, rpm2: null },
    ];
    expect(windowedHistory(history, 5, 1100)).toEqual([history[1]]);
  });
});

describe("curvesEqual", () => {
  it("compares curve contents, not array identity", () => {
    expect(curvesEqual([[50, 0], [80, 100]], [[50, 0], [80, 100]])).toBe(true);
    expect(curvesEqual([[50, 0], [80, 100]], [[50, 10], [80, 100]])).toBe(false);
  });
});
