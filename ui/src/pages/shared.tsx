import { useEffect, useState, type ReactNode } from "react";
import { useConnection } from "../stores/connectionStore";
import { useConfig } from "../stores/configStore";
import { EmptyState, Skeleton } from "../components/ui";
export function useResource<T>(
  fetcher: () => Promise<T>,
  dependencies: unknown[] = [],
  interval = 0,
) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState<string | null>(null),
    [nonce, setNonce] = useState(0);
  useEffect(() => {
    let active = true,
      pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await fetcher();
        if (active) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (active) setError((e as Error).message);
      } finally {
        pending = false;
      }
    };
    void load();
    const timer = interval ? setInterval(load, interval) : undefined;
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [...dependencies, nonce]);
  return { data, error, retry: () => setNonce((n) => n + 1) };
}
export function ResourceState({
  resource,
  children,
}: {
  resource: { data: unknown; error: string | null; retry: () => void };
  children: ReactNode;
}) {
  return resource.error ? (
    <div role="alert">
      <p>{resource.error}</p>
      <button onClick={resource.retry}>Retry</button>
    </div>
  ) : resource.data === null ? (
    <Skeleton />
  ) : (
    <>{children}</>
  );
}
export function Feature({
  name,
  feature,
  children,
}: {
  name: string;
  feature: string;
  children: ReactNode;
}) {
  const supported = useConnection((s) =>
    s.capabilities?.features.includes(feature),
  );
  return supported ? (
    <>{children}</>
  ) : (
    <EmptyState>
      This daemon does not support {name}. Update fan-daemon to enable this
      page.
    </EmptyState>
  );
}
export function useDisabled() {
  const status = useConnection((s) => s.status),
    busy = useConfig((s) => s.busy);
  return status !== "connected" || busy;
}
export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type="number"
        required
        value={value}
        min={min}
        max={max}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}
