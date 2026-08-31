import { describe, expect, it, vi } from "vitest";
import { call } from "./bridge";

describe("bridge", () => {
  it("rejects without a native bridge", async () => {
    window.__fanControl = undefined;
    await expect(call("snapshot")).rejects.toThrow(/native bridge/);
  });

  it("forwards method and params", async () => {
    window.__fanControl = { call: vi.fn().mockResolvedValue({ ok: true }), event: vi.fn() };
    await expect(call("set", { fan: 1, pct: 40 })).resolves.toEqual({ ok: true });
    expect(window.__fanControl.call).toHaveBeenCalledWith("set", { fan: 1, pct: 40 });
  });
});
