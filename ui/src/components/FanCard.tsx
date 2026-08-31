import type { Snapshot } from "../types";

const NAMES: Record<number, string> = { 1: "CPU fan", 2: "GPU fan", 3: "Aux fan" };
const PRESETS = [
  { label: "Stop", value: 0 },
  { label: "Quiet", value: 25 },
  { label: "Medium", value: 50 },
  { label: "High", value: 75 },
  { label: "Max", value: 100 },
];

type Props = {
  snap: Snapshot;
  selected: number;
  onSelect: (fan: number) => void;
  onSet: (fan: number, pct: number) => void;
  onLinked: (linked: boolean) => void;
};

export default function FanCards({ snap, selected, onSelect, onSet, onLinked }: Props) {
  const fans = snap.fans?.length ? snap.fans : [1, 2];
  const manual = snap.mode === "manual";
  return (
    <section className="panel">
      <div className="sectionHead">
        <h2>Fans</h2>
        <label className="row">
          <input type="checkbox" checked={snap.linked} onChange={(e) => onLinked(e.target.checked)} />
          Link fans
        </label>
      </div>
      <div className="fans">
        {fans.map((fan) => {
          const duty = Number(snap[`fan${fan}` as "fan1"] ?? 0);
          const target = Number(snap.targets[String(fan)] ?? 0);
          const rpm = snap[`rpm${fan}` as "rpm1"];
          return (
            <div className="fan" key={fan} onClick={() => onSelect(fan)}>
              <div className="fanHead">
                <div>
                  <div className="fanName">{NAMES[fan] ?? `Fan ${fan}`}</div>
                  <div className="fanMeta">
                    Duty {duty}%{rpm ? ` · ${rpm} RPM` : ""} {selected === fan ? " · selected" : ""}
                  </div>
                </div>
                <div className="row">
                  <div className="gauge" style={{ ["--duty" as string]: duty }} aria-valuenow={duty} aria-label={`${NAMES[fan]} duty`}>
                    <span>{duty}%</span>
                  </div>
                  <div className="fanValue">
                    {target}% <small>set</small>
                  </div>
                </div>
              </div>
              <input
                type="range"
                min={0}
                max={100}
                value={target}
                disabled={!manual}
                aria-label={`${NAMES[fan] ?? `Fan ${fan}`} speed`}
                style={{ ["--fill" as string]: `${target}%` }}
                onChange={(e) => onSet(fan, Number(e.target.value))}
              />
            </div>
          );
        })}
      </div>
      <div className="preset">
        {PRESETS.map((preset) => (
          <button key={preset.value} disabled={!manual} onClick={() => onSet(fans[0] ?? 1, preset.value)}>
            {preset.label}
          </button>
        ))}
      </div>
    </section>
  );
}
