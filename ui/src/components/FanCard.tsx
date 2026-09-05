import { useEffect, useRef, useState } from "react";
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
  const [draft, setDraft] = useState<number | null>(null);
  const dragging = useRef(false);
  const timer = useRef<number>(0);
  const pending = useRef<{ fan: number; pct: number } | null>(null);

  useEffect(() => {
    return () => window.clearTimeout(timer.current);
  }, []);

  function flush() {
    const next = pending.current;
    pending.current = null;
    if (next) onSet(next.fan, next.pct);
  }

  function queueSet(fan: number, pct: number) {
    pending.current = { fan, pct };
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      flush();
      if (!dragging.current) setDraft(null);
    }, 180);
  }

  function displayTarget(fan: number) {
    if (draft != null && (snap.linked || fan === selected)) return draft;
    return Number(snap.targets[String(fan)] ?? 0);
  }

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
          const target = displayTarget(fan);
          const rpm = snap[`rpm${fan}` as "rpm1"];
          return (
            <div className={`fan${selected === fan ? " selected" : ""}`} key={fan} onClick={() => onSelect(fan)}>
              <div className="fanHead">
                <div>
                  <div className="fanName">{NAMES[fan] ?? `Fan ${fan}`}</div>
                  <div className="fanMeta">
                    {rpm ? `${rpm} RPM` : "No tach"}
                    {selected === fan ? " · selected" : ""}
                  </div>
                </div>
                <div className="row">
                  <div className="gauge" style={{ ["--duty" as string]: String(target) }} aria-valuenow={target} aria-label={`${NAMES[fan]} set duty`}>
                    <span>{target}%</span>
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
                onPointerDown={() => {
                  dragging.current = true;
                  onSelect(fan);
                }}
                onPointerUp={() => {
                  dragging.current = false;
                  flush();
                  setDraft(null);
                }}
                onChange={(e) => {
                  const pct = Number(e.target.value);
                  setDraft(pct);
                  queueSet(fan, pct);
                }}
              />
            </div>
          );
        })}
      </div>
      <div className="preset">
        {PRESETS.map((preset) => (
          <button
            key={preset.value}
            disabled={!manual}
            onClick={() => {
              setDraft(preset.value);
              onSet(fans[0] ?? 1, preset.value);
            }}
          >
            {preset.label}
          </button>
        ))}
      </div>
    </section>
  );
}
