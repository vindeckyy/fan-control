import type { Snapshot } from "../types";

const MODES: Array<{ label: string; mode?: Snapshot["mode"]; profile?: string }> = [
  { label: "Manual", mode: "manual" },
  { label: "Silent", profile: "silent" },
  { label: "Balanced", profile: "balanced" },
  { label: "Performance", profile: "performance" },
  { label: "Custom", profile: "custom" },
  { label: "EC Auto", mode: "released" },
];

type Props = {
  snap: Snapshot;
  onMode: (mode: Snapshot["mode"]) => void;
  onProfile: (profile: string) => void;
};

export default function ModeBar({ snap, onMode, onProfile }: Props) {
  return (
    <div className="modebar" id="modes">
      {MODES.map((item) => {
        const active = item.mode
          ? snap.mode === item.mode
          : snap.mode === "curve" && snap.profile === item.profile;
        return (
          <button
            key={item.label}
            className={active ? "active" : ""}
            onClick={() => (item.mode ? onMode(item.mode) : onProfile(item.profile!))}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
