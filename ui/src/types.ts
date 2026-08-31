export type CurvePoint = [number, number];

export type Sensor = {
  name: string;
  label: string;
  temp: number;
};

export type Snapshot = {
  mode: "manual" | "curve" | "released";
  profile: string;
  max_duty: number;
  hysteresis: number;
  critical_temp: number;
  linked: boolean;
  curve: CurvePoint[];
  custom_curve: CurvePoint[];
  curve_cpu: CurvePoint[] | null;
  curve_gpu: CurvePoint[] | null;
  named_curves: Record<string, CurvePoint[]>;
  alerts: { desktop: boolean };
  cpu_sensor: { name: string; label: string } | null;
  gpu_sensor: { name: string; label: string } | null;
  theme: "dark" | "light";
  targets: Record<string, number>;
  temps: Sensor[];
  primary_temp: number | null;
  gpu_temp: number | null;
  control_temp: number | null;
  fans: number[];
  backend: string;
  demo: boolean;
  profiles: Record<string, CurvePoint[]>;
  fault_missing: number;
  critical_active: boolean;
  fan1: number;
  fan2: number;
  fan3: number;
  rpm1: number | null;
  rpm2: number | null;
  rpm3: number | null;
  updated: number;
};

export type HistoryPoint = {
  time: number;
  temp: number | null;
  gpu_temp: number | null;
  control_temp: number | null;
  fan1: number;
  fan2: number;
  fan3?: number;
  rpm1: number | null;
  rpm2: number | null;
  rpm3?: number | null;
};
