import { describe, expect, it } from "vitest";
import { interpolate, normalizeCurve } from "./curveMath";

describe("normalizeCurve", () => {
  it("sorts, clamps, and deduplicates", () => {
    expect(normalizeCurve([[80, 140], [50, 0], [95, 250], [50, 20]])).toEqual([
      [50, 20],
      [80, 100],
      [95, 100],
    ]);
  });

  it("rejects a single point", () => {
    expect(() => normalizeCurve([[50, 10]])).toThrow(/two unique/);
  });
});

describe("interpolate", () => {
  it("lerps between points", () => {
    expect(interpolate(65, [[50, 0], [80, 100], [95, 100]])).toBe(50);
  });
});
