export async function call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  if (!window.__fanControl) {
    throw new Error("native bridge unavailable");
  }
  return window.__fanControl.call(method, params) as Promise<T>;
}

export function onFanEvent(handler: (name: string, payload: unknown) => void): () => void {
  const listener = (event: Event) => {
    const detail = (event as CustomEvent).detail as { name: string; payload: unknown };
    handler(detail.name, detail.payload);
  };
  window.addEventListener("fan-event", listener);
  return () => window.removeEventListener("fan-event", listener);
}
