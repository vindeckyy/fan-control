import type { HistoryPoint, Snapshot } from "./types";

const LIVE_KEYS = [
  "fan1",
  "fan2",
  "fan3",
  "rpm1",
  "rpm2",
  "rpm3",
  "temps",
  "primary_temp",
  "gpu_temp",
  "control_temp",
  "targets",
  "mode",
  "profile",
  "updated",
  "critical_active",
  "fault_missing",
  "linked",
] as const;

export type LiveTelemetry = Pick<Snapshot, (typeof LIVE_KEYS)[number]>;

export function applyLive(snap: Snapshot | null, live: LiveTelemetry): Snapshot {
  if (!snap) {
    return live as Snapshot;
  }
  return { ...snap, ...live };
}

export function windowedHistory(history: HistoryPoint[], windowMin: number, nowSec: number): HistoryPoint[] {
  const cutoff = nowSec - windowMin * 60;
  return history.filter((point) => point.time >= cutoff);
}

export function curvesEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
