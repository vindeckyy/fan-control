import { useState } from "react";
import { client, request, type Decision } from "../client";
import { Panel, Select, TempBadge, EmptyState } from "../components/ui";
import { tell } from "../stores/uiStore";
import { Feature, ResourceState, useResource } from "./shared";
function Decisions() {
  const resource = useResource(client.decisions, [], 2000),
    [fan, setFan] = useState("all");
  const rows =
    resource.data?.decisions
      .filter((d) => fan === "all" || d.fan_id === fan)
      .slice(-60)
      .reverse() || [];
  return (
    <Panel title="Recent control decisions">
      <Select label="Filter fan" value={fan} onChange={setFan}>
        <option value="all">All fans</option>
        {["fan1", "fan2", "fan3"].map((f) => (
          <option key={f}>{f}</option>
        ))}
      </Select>
      <ResourceState resource={resource}>
        {rows.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time / fan</th>
                  <th>Source / curve</th>
                  <th>Requested</th>
                  <th>Bounded</th>
                  <th>Final</th>
                  <th>Write</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d: Decision, i) => (
                  <tr key={i}>
                    <td>
                      {new Date(d.timestamp).toLocaleTimeString()}
                      <br />
                      {d.fan_id}
                    </td>
                    <td>
                      {d.sensor_id || "Manual"} ·{" "}
                      <TempBadge value={d.source_temp} />
                      <br />
                      <small>{d.configured_curve || d.policy_source}</small>
                    </td>
                    <td>{d.requested_duty ?? "n/a"}%</td>
                    <td>{d.bounded_duty ?? "n/a"}%</td>
                    <td>
                      {d.final_duty ?? "n/a"}%
                      {d.safety_override && (
                        <b className="critical"> Critical</b>
                      )}
                    </td>
                    <td>
                      {d.write_performed
                        ? "Written"
                        : d.write_suppressed_reason || "No write"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState>
            The daemon has not recorded a control decision yet.
          </EmptyState>
        )}
      </ResourceState>
    </Panel>
  );
}
export default function Diagnostics() {
  const resource = useResource(
    () => request<Record<string, unknown>>("diagnostics.snapshot"),
    [],
    5000,
  );
  return (
    <>
      <h1>Diagnostics</h1>
      <div className="actions diagnostic-actions">
        <button
          onClick={() => {
            void request("diagnostics.copy").catch((e) => tell(e.message));
          }}
        >
          Copy diagnostics
        </button>
        <button
          onClick={() => {
            void request("diagnostics.pick_export").catch((e) =>
              tell(e.message),
            );
          }}
        >
          Export diagnostics
        </button>
        <button onClick={resource.retry}>Refresh diagnostics</button>
      </div>
      <ResourceState resource={resource}>
        <div className="two-col">
          {Object.entries(resource.data || {})
            .filter(([key]) => !["sensors", "runtime"].includes(key))
            .map(([key, value]) => (
              <Panel key={key} title={key[0].toUpperCase() + key.slice(1)}>
                {Array.isArray(value) && !value.length ? (
                  <p className="muted">No {key} reported.</p>
                ) : (
                  <dl className="trace">
                    {typeof value === "object" &&
                    value !== null &&
                    !Array.isArray(value) ? (
                      Object.entries(value).map(([k, v]) => (
                        <div key={k}>
                          <dt>{k.replaceAll("_", " ")}</dt>
                          <dd>
                            {typeof v === "object"
                              ? JSON.stringify(v)
                              : String(v)}
                          </dd>
                        </div>
                      ))
                    ) : (
                      <p>{JSON.stringify(value)}</p>
                    )}
                  </dl>
                )}
              </Panel>
            ))}
        </div>
      </ResourceState>
      <Feature name="decision tracing" feature="decision_trace">
        <Decisions />
      </Feature>
    </>
  );
}
