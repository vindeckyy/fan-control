import type { Snapshot } from "../types";

type Props = { snap: Snapshot | null };

export default function Banners({ snap }: Props) {
  if (!snap) {
    return <div className="banner">Connecting to the native bridge…</div>;
  }
  return (
    <>
      {snap.demo && <div className="banner demo">Simulated hardware — demo mode</div>}
      {snap.critical_active && (
        <div className="banner critical" role="alert">
          Both fans at 100% because control temp ≥ critical
        </div>
      )}
      {snap.fault_missing > 0 && snap.fault_missing < 3 && (
        <div className="banner">Temperature missing ({snap.fault_missing}/3) — firmware handoff soon</div>
      )}
      {snap.fault_missing >= 3 && <div className="banner">Temperature unavailable — control returned to firmware</div>}
    </>
  );
}
