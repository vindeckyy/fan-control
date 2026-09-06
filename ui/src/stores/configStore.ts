import { create } from "zustand";
import { client, DaemonError, type Config, type FullSnapshot } from "../client";
import { useConnection } from "./connectionStore";
import { tell } from "./uiStore";
export const useConfig = create<{
  config: Config | null;
  snapshot: FullSnapshot | null;
  busy: boolean;
}>(() => ({ config: null, snapshot: null, busy: false }));
export function receiveSnapshot(snapshot: FullSnapshot) {
  useConfig.setState({ snapshot, config: snapshot.config || null });
}
export async function refreshConfig() {
  receiveSnapshot(await client.snapshot());
}
export async function mutate(
  method: string,
  params: Record<string, unknown>,
  message = "Changes saved",
) {
  const { config, busy } = useConfig.getState();
  if (!config || busy || useConnection.getState().status !== "connected")
    throw new DaemonError(
      "Wait for a current daemon connection",
      "UNAVAILABLE",
    );
  useConfig.setState({ busy: true });
  try {
    const reply = await client.mutate(method, params, config.revision);
    if (reply.config) useConfig.setState({ config: reply.config });
    else await refreshConfig();
    tell(message);
    return reply;
  } catch (error) {
    if (error instanceof DaemonError && error.code === "REVISION_CONFLICT") {
      await refreshConfig();
      tell("Configuration changed elsewhere. Review it and try again.");
    } else tell((error as Error).message);
    throw error;
  } finally {
    useConfig.setState({ busy: false });
  }
}
export function act(
  method: string,
  params: Record<string, unknown>,
  message?: string,
) {
  void mutate(method, params, message).catch(() => {});
}
