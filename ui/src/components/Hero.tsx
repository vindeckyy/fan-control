import type { Snapshot } from "../types";
import Sparkline from "./Sparkline";
import ModeBar from "./ModeBar";

type Props = {
  snap: Snapshot;
  spark: Array<number | null>;
  onMode: (mode: Snapshot["mode"]) => void;
  onProfile: (profile: string) => void;
};

export default function Hero({ snap, spark, onMode, onProfile }: Props) {
  const temp = snap.control_temp;
  return (
    <section className="panel">
      <div className="hero">
        <div>
          <div className="eyebrow">Control temperature</div>
          <div className="tempNow">
            <span>{temp == null ? "--" : temp.toFixed(1)}</span>
            <small>°C</small>
          </div>
          <div className="subtemps">
            <span>CPU {snap.primary_temp == null ? "--" : snap.primary_temp.toFixed(1)}°C</span>
            <span>GPU {snap.gpu_temp == null ? "--" : snap.gpu_temp.toFixed(1)}°C</span>
          </div>
        </div>
        <div className="heroRight">
          <Sparkline values={spark} />
          <div className="eyebrow">{snap.mode === "released" ? "EC Auto" : snap.mode === "curve" ? snap.profile : "Manual"}</div>
        </div>
      </div>
      <ModeBar snap={snap} onMode={onMode} onProfile={onProfile} />
    </section>
  );
}
