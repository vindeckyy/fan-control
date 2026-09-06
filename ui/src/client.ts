import { call } from "./bridge";
import type { CurvePoint, Snapshot } from "./types";

export type FanId = "fan1" | "fan2" | "fan3";
export type Control = {
  type: "linked" | "profile" | "curve" | "manual";
  curve_ref?: string | null;
  target?: number;
  temp_source?: string;
};
export type Fan = {
  name: string;
  enabled: boolean;
  control: Control;
  min_duty: number;
  max_duty: number;
};
export type Curve = { name: string; temp_source: string; points: CurvePoint[] };
export type Rule = {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  trigger: {
    type: string;
    sensor?: string;
    value?: number;
    fan?: string;
    start?: string;
    end?: string;
    days?: string[];
  };
  condition: { sustain_ticks: number; cooldown_seconds: number };
  action: {
    type: string;
    profile?: string;
    mode?: string;
    pct?: number;
    fan?: string;
    message?: string;
  };
};
export type Schedule = {
  enabled: boolean;
  timezone: string;
  items: {
    id: string;
    days: string[];
    start: string;
    end: string;
    profile?: string;
    mode?: string;
  }[];
};
export type Config = {
  version: number;
  revision: number;
  mode: string;
  profile: string;
  fans: Record<FanId, Fan>;
  curves: Record<string, Curve>;
  rules: Rule[];
  schedule: Schedule;
  safety: {
    critical_temp: number;
    global_max_duty: number;
    hysteresis: number;
    firmware_fallback: boolean;
  };
  display: { theme: "dark" | "light" | "system"; temperature_unit: "c" | "f" };
  notifications: { enabled: boolean; critical_temp: boolean };
  sensors: { cpu_pin: unknown; gpu_pin: unknown; pinned: string[] };
  history: { persist: boolean; retention_days: number };
};
export type Live = Partial<Snapshot> & {
  seq: number;
  timestamp: number;
  config_revision: number;
  effective_mode: string;
  effective_profile: string;
  configured_mode: string;
  configured_profile: string;
  policy_source: string;
  active_rules: string[];
  backend_state: string;
  backend_error?: string | null;
};
export type FullSnapshot = Snapshot &
  Live & { config?: Config; warnings: string[] };
export type Capabilities = {
  version: string;
  protocol_version: number;
  schema_version: number;
  features: string[];
  backend: string;
  fan_count: number;
  demo: boolean;
};
export type Mutation = {
  ok: boolean;
  config_revision: number;
  config?: Config;
  changed?: Record<string, unknown>;
};
export type SensorRecord = {
  id: string;
  name: string;
  label: string;
  value: number | null;
  source: string;
  available: boolean;
  runtime_path: string | null;
  aliases?: string[];
};
export type Decision = {
  timestamp: string;
  fan_id: FanId;
  sensor_id: string | null;
  source_temp: number | null;
  configured_curve: string | null;
  requested_duty: number | null;
  bounded_duty: number | null;
  final_duty: number | null;
  policy_source: string;
  safety_override: boolean;
  write_performed: boolean;
  write_suppressed_reason: string | null;
};
export type History = {
  samples: {
    ts: number;
    hottest_temp: number | null;
    critical: boolean;
    effective_profile: string;
  }[];
  fans: { ts: number; fan_id: string; duty: number; rpm: number | null }[];
  sensors: { ts: number; sensor_id: string; value: number }[];
  degraded: boolean;
};

export class DaemonError extends Error {
  constructor(
    message: string,
    public code = "BACKEND_ERROR",
  ) {
    super(message);
  }
}
export async function request<T>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  try {
    return await call<T>(method, params);
  } catch (error) {
    const e = error as { message?: string; code?: string };
    throw new DaemonError(e.message || String(error), e.code);
  }
}
export const client = {
  capabilities: () => request<Capabilities>("capabilities"),
  snapshot: () => request<FullSnapshot>("snapshot"),
  live: () => request<Live>("live"),
  mutate: (method: string, params: Record<string, unknown>, revision: number) =>
    request<Mutation>(method, { ...params, expected_revision: revision }),
  sensors: () =>
    request<{
      sensors: SensorRecord[];
      hottest: string | null;
      aliases: Record<string, string | null>;
    }>("sensors.list"),
  decisions: () => request<{ decisions: Decision[] }>("diagnostics.decisions"),
  history: (params: Record<string, unknown>) =>
    request<History>("history.query", params),
  rules: () => request<{ rules: (Rule & { state: string })[] }>("rules.list"),
};
