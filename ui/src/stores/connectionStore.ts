import { create } from "zustand";
import type { Capabilities } from "../client";
export type ConnectionStatus =
  | "connected"
  | "reconnecting"
  | "stale"
  | "unsupported-version"
  | "daemon-error";
export const useConnection = create<{
  status: ConnectionStatus;
  capabilities: Capabilities | null;
  lastLive: number;
  error: string | null;
}>(() => ({
  status: "reconnecting",
  capabilities: null,
  lastLive: 0,
  error: null,
}));
export function checkStale(now = Date.now()) {
  const state = useConnection.getState();
  if (state.status === "connected" && now - state.lastLive > 5000)
    useConnection.setState({ status: "stale" });
}
