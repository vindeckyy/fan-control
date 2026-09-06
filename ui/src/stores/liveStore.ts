import { create } from "zustand";
import type { Live } from "../client";
export const useLive = create<{ live: Live | null; history: Live[] }>(() => ({
  live: null,
  history: [],
}));
export function receiveLive(live: Live) {
  useLive.setState((state) => ({
    live,
    history: [
      ...state.history.filter(
        (p) =>
          p.timestamp >= live.timestamp - 1800 && p.timestamp <= live.timestamp,
      ),
      live,
    ].slice(-3600),
  }));
}
