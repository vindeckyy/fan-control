export function toDisplay(celsius: number, unit: "c" | "f") {
  return unit === "f" ? (celsius * 9) / 5 + 32 : celsius;
}
export function temperature(
  value: number | null | undefined,
  unit: "c" | "f" = "c",
) {
  return value == null || !Number.isFinite(value)
    ? "Unavailable"
    : `${toDisplay(value, unit).toFixed(1)}°${unit.toUpperCase()}`;
}
