import type { Snapshot } from "../types";

type Props = {
  snap: Snapshot;
  diagnose: string;
  onConfig: (patch: Record<string, unknown>) => void;
  onDiagnose: () => void;
  onEcAuto: () => void;
  onTheme: () => void;
};

export default function SafetyDrawer({ snap, diagnose, onConfig, onDiagnose, onEcAuto, onTheme }: Props) {
  return (
    <section className="panel span2">
      <details>
        <summary>Safety &amp; preferences</summary>
        <div className="settings" style={{ marginTop: 12 }}>
          <div className="setting">
            <label>Maximum duty<span>Noise cap; critical cooling bypasses it</span></label>
            <input type="number" min={20} max={100} value={snap.max_duty} onChange={(e) => onConfig({ max_duty: Number(e.target.value) })} />
          </div>
          <div className="setting">
            <label>Curve hysteresis<span>Prevents rapid speed hunting</span></label>
            <input type="number" min={0} max={30} value={snap.hysteresis} onChange={(e) => onConfig({ hysteresis: Number(e.target.value) })} />
          </div>
          <div className="setting">
            <label>Critical temperature<span>Forces safe maximum duty</span></label>
            <input type="number" min={70} max={110} value={snap.critical_temp} onChange={(e) => onConfig({ critical_temp: Number(e.target.value) })} />
          </div>
          <div className="setting">
            <label>Desktop alerts<span>Notification at critical temperature</span></label>
            <input type="checkbox" checked={snap.alerts?.desktop} onChange={(e) => onConfig({ alerts: { desktop: e.target.checked } })} />
          </div>
          <div className="setting">
            <label>Theme<span>Follows in-app override, persisted in config</span></label>
            <button className="secondary" onClick={onTheme}>{snap.theme === "dark" ? "Light" : "Dark"}</button>
          </div>
        </div>
        <div className="actions">
          <button className="secondary" onClick={onEcAuto}>Return to EC Auto</button>
          <button className="secondary" onClick={onDiagnose}>Diagnose</button>
        </div>
        {diagnose && <pre className="diag">{diagnose}</pre>}
      </details>
    </section>
  );
}
