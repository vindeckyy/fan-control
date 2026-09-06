import { client } from "../client";
import { Panel, TempBadge, Toggle, Sparkline } from "../components/ui";
import { act, useConfig } from "../stores/configStore";
import { useLive } from "../stores/liveStore";
import { Feature, ResourceState, useDisabled, useResource } from "./shared";
function Explorer() {
  const resource = useResource(client.sensors, [], 2000),
    config = useConfig((s) => s.config)!,
    disabled = useDisabled(),
    history = useLive((s) => s.history);
  const records = resource.data?.sensors || [],
    groups = [...new Set(records.map((s) => s.name))];
  const hottest = records.find((s) => s.id === resource.data?.hottest);
  return (
    <ResourceState resource={resource}>
      <Panel title="Control sources">
        <p>
          Hottest discovered sensor:{" "}
          {hottest ? (
            <>
              {hottest.label} · <TempBadge value={hottest.value} />
            </>
          ) : (
            "No temperature sensors available"
          )}
        </p>
        <p className="muted">
          CPU and GPU are policy aliases. The linked control source is the
          hotter of those two aliases; other hardware sensors can be assigned to
          a named curve.
        </p>
        <div className="actions">
          <button
            disabled={disabled}
            onClick={() =>
              act("config", { cpu_sensor: null, gpu_sensor: null })
            }
          >
            Use automatic CPU / GPU selection
          </button>
        </div>
      </Panel>
      {groups.map((group) => (
        <Panel key={group} title={group}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Channel / identity</th>
                  <th>Temperature</th>
                  <th>Last 60 seconds</th>
                  <th>Use for control</th>
                </tr>
              </thead>
              <tbody>
                {records
                  .filter((s) => s.name === group)
                  .map((s) => (
                    <tr key={s.id}>
                      <td>
                        <b>{s.label}</b>
                        <br />
                        <small>
                          {s.id}
                          <br />
                          {s.source} ·{" "}
                          {s.available ? "Available" : "Unavailable"}
                        </small>
                        <Toggle
                          label="Pin to history"
                          disabled={disabled}
                          checked={config.sensors.pinned.includes(s.id)}
                          onChange={(on) =>
                            act("config", {
                              sensors: {
                                pinned: on
                                  ? [...config.sensors.pinned, s.id]
                                  : config.sensors.pinned.filter(
                                      (id) => id !== s.id,
                                    ),
                              },
                            })
                          }
                        />
                      </td>
                      <td>
                        <TempBadge value={s.value} />
                        {s.id === resource.data?.hottest && (
                          <small> · Hottest</small>
                        )}
                      </td>
                      <td>
                        <Sparkline
                          label={`${s.label} over sixty seconds`}
                          values={history
                            .filter((p) => p.timestamp > Date.now() / 1000 - 60)
                            .map(
                              (p) =>
                                p.temps?.find(
                                  (t) =>
                                    (t as unknown as { id: string }).id ===
                                    s.id,
                                )?.temp,
                            )}
                        />
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            disabled={
                              disabled || config.sensors.cpu_pin === s.id
                            }
                            onClick={() => act("config", { cpu_sensor: s.id })}
                          >
                            CPU
                            {s.id === config.sensors.cpu_pin ? " selected" : ""}
                          </button>
                          <button
                            disabled={
                              disabled || config.sensors.gpu_pin === s.id
                            }
                            onClick={() => act("config", { gpu_sensor: s.id })}
                          >
                            GPU
                            {s.id === config.sensors.gpu_pin ? " selected" : ""}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ))}
    </ResourceState>
  );
}
export default function Sensors() {
  return (
    <>
      <h1>Sensors</h1>
      <Feature name="the sensor explorer" feature="sensors_v2">
        <Explorer />
      </Feature>
    </>
  );
}
