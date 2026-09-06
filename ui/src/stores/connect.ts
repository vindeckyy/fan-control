import { onFanEvent } from "../bridge";
import { client, type FullSnapshot, type Live } from "../client";
import { checkStale, useConnection } from "./connectionStore";
import { receiveSnapshot, refreshConfig, useConfig } from "./configStore";
import { receiveLive, useLive } from "./liveStore";
import { tell } from "./uiStore";

export function connect() {
  let stopped = false,
    connecting = false,
    refreshing = false;
  async function negotiate() {
    if (connecting || stopped) return;
    connecting = true;
    try {
      const capabilities = await client.capabilities();
      if (stopped) return;
      useConnection.setState({ capabilities });
      if (
        capabilities.protocol_version !== 2 ||
        capabilities.schema_version !== 2
      ) {
        useConnection.setState({
          status: "unsupported-version",
          error:
            "This daemon version is not supported. Update the daemon and reopen Fan Control.",
        });
        return;
      }
      const snapshot = await client.snapshot();
      if (stopped) return;
      receiveSnapshot(snapshot);
      accept(await client.live());
    } catch (error) {
      if (!stopped) {
        const message = (error as Error).message;
        const legacyDaemon = /unknown method ['"]?capabilities/i.test(message);
        useConnection.setState({
          status: legacyDaemon ? "unsupported-version" : "reconnecting",
          error: legacyDaemon
            ? "The running daemon is older than this app. Update and restart fan-daemon to connect."
            : message,
        });
      }
    } finally {
      connecting = false;
    }
  }
  function accept(live: Live) {
    if (stopped) return;
    const previous = useLive.getState().live;
    if (
      previous &&
      live.seq === previous.seq &&
      live.timestamp === previous.timestamp
    )
      return;
    receiveLive(live);
    useConnection.setState({
      status: "connected",
      lastLive: Date.now(),
      error: null,
    });
    const config = useConfig.getState().config;
    if (
      !refreshing &&
      config &&
      (live.config_revision !== config.revision ||
        (previous && live.seq < previous.seq))
    ) {
      refreshing = true;
      void refreshConfig()
        .catch((e) => tell(e.message))
        .finally(() => {
          refreshing = false;
        });
    }
  }
  const unsubscribe = onFanEvent((name, payload) => {
    if (name === "live") {
      if (useConnection.getState().status !== "connected" && !connecting)
        void negotiate();
      else if (useConnection.getState().capabilities?.protocol_version === 2)
        accept(payload as Live);
    } else if (name === "snapshot" && !useConfig.getState().busy)
      receiveSnapshot(payload as FullSnapshot);
    else if (name === "toast") tell((payload as { message: string }).message);
  });
  void negotiate();
  const timer = setInterval(() => {
    checkStale();
    if (useConnection.getState().status !== "connected") void negotiate();
  }, 2000);
  return () => {
    stopped = true;
    clearInterval(timer);
    unsubscribe();
  };
}
