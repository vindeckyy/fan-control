import type { Snapshot } from "../types";

type Props = {
  snap: Snapshot;
  onPin: (kind: "cpu_sensor" | "gpu_sensor", pin: { name: string; label: string } | null) => void;
};

export default function SensorRail({ snap, onPin }: Props) {
  const groups = new Map<string, Snapshot["temps"]>();
  for (const sensor of snap.temps) {
    const list = groups.get(sensor.name) ?? [];
    list.push(sensor);
    groups.set(sensor.name, list);
  }
  return (
    <aside className="panel">
      <div className="sectionHead">
        <h2>Live sensors</h2>
        <span>{snap.temps.length} detected</span>
      </div>
      <div className="sensorList">
        {snap.temps.length === 0 && <div className="empty">No hwmon sensors found</div>}
        {[...groups.entries()].map(([chip, sensors]) => (
          <div key={chip}>
            <div className="eyebrow">{chip}</div>
            {sensors.map((sensor) => {
              const hot = sensor.temp >= snap.critical_temp;
              const cpuPinned = snap.cpu_sensor?.name === sensor.name && snap.cpu_sensor?.label === sensor.label;
              const gpuPinned = snap.gpu_sensor?.name === sensor.name && snap.gpu_sensor?.label === sensor.label;
              return (
                <div className={`sensor${hot ? " hot" : ""}`} key={`${sensor.name}-${sensor.label}`}>
                  <span>
                    {sensor.label}
                    {hot ? " · critical" : ""}
                    {cpuPinned ? " · CPU pin" : ""}
                    {gpuPinned ? " · GPU pin" : ""}
                  </span>
                  <b>{sensor.temp.toFixed(1)}°C</b>
                  <span className="row">
                    <button type="button" onClick={() => onPin("cpu_sensor", cpuPinned ? null : { name: sensor.name, label: sensor.label })}>
                      CPU
                    </button>
                    <button type="button" onClick={() => onPin("gpu_sensor", gpuPinned ? null : { name: sensor.name, label: sensor.label })}>
                      GPU
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </aside>
  );
}
