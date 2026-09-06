import type { CurvePoint } from "./types";

export function normalizeCurve(curve: CurvePoint[]): CurvePoint[] {
  const points = new Map<number, number>();
  for (const [temp, duty] of curve) {
    if (!Number.isFinite(temp) || !Number.isFinite(duty)) continue;
    points.set(
      Math.max(0, Math.min(150, Math.round(temp))),
      Math.max(0, Math.min(100, Math.round(duty))),
    );
  }
  const result = [...points.entries()].sort((a, b) => a[0] - b[0]);
  if (result.length < 2) {
    throw new Error("curve needs at least two unique temperatures");
  }
  return result;
}

export function interpolate(temp: number, curve: CurvePoint[]): number {
  if (temp <= curve[0][0]) return curve[0][1];
  if (temp >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];
  for (let i = 0; i < curve.length - 1; i += 1) {
    const [t0, d0] = curve[i];
    const [t1, d1] = curve[i + 1];
    if (t0 <= temp && temp < t1) {
      return d0 + ((d1 - d0) * (temp - t0)) / (t1 - t0);
    }
  }
  return curve[curve.length - 1][1];
}

export function pathForCurve(
  curve: CurvePoint[],
  width: number,
  height: number,
  maxTemp = 110,
): string {
  return curve
    .map(([temp, duty], index) => {
      const x = (Math.min(maxTemp, temp) / maxTemp) * width;
      const y = height - (duty / 100) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function validateCurve(points: CurvePoint[]): string | null {
  if (points.length < 2) return "Keep at least two points.";
  for (let i = 0; i < points.length; i++) {
    const [temp, duty] = points[i];
    if (
      !Number.isFinite(temp) ||
      !Number.isFinite(duty) ||
      temp < 0 ||
      temp > 150 ||
      duty < 0 ||
      duty > 100
    )
      return "Temperatures must be 0–150°C and duty 0–100%.";
    if (i && temp <= points[i - 1][0])
      return "Point temperatures must increase.";
  }
  return null;
}
export function movePoint(
  points: CurvePoint[],
  index: number,
  temp: number,
  duty: number,
): CurvePoint[] {
  if (!Number.isFinite(temp) || !Number.isFinite(duty)) return points;
  const low = index ? points[index - 1][0] + 1 : 0;
  const high = index < points.length - 1 ? points[index + 1][0] - 1 : 150;
  if (low > high) return points;
  return points.map((p, i) =>
    i === index
      ? [
          Math.max(low, Math.min(high, Math.round(temp))),
          Math.max(0, Math.min(100, Math.round(duty))),
        ]
      : [...p],
  );
}
export function insertPoint(points: CurvePoint[], temp: number): CurvePoint[] {
  temp = Math.max(0, Math.min(150, Math.round(temp)));
  if (points.some((p) => p[0] === temp)) return points;
  return [
    ...points,
    [temp, Math.round(interpolate(temp, points))] as CurvePoint,
  ].sort((a, b) => a[0] - b[0]);
}
export function deletePoint(points: CurvePoint[], index: number): CurvePoint[] {
  return points.length > 2 ? points.filter((_, i) => i !== index) : points;
}
