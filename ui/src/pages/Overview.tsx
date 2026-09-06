import { Link } from "react-router-dom";
import { client, type FanId } from "../client";
import {
  Panel,
  Stat,
  TempBadge,
  Sparkline,
  DutyBar,
  Select,
  ConfirmDialog,
} from "../components/ui";
import { act, useConfig } from "../stores/configStore";
import { useLive } from "../stores/liveStore";
import { useDisabled, useResource } from "./shared";
export default function Overview() {
  const config = useConfig((s) => s.config)!,
    snapshot = useConfig((s) => s.snapshot),
    live = useLive((s) => s.live),
    history = useLive((s) => s.history),
    disabled = useDisabled();
  const traces = useResource(client.decisions, [], 2000);
  const present = snapshot?.fans || [];
  return (
    <>
      <h1>Overview</h1>
      <div className="two-col">
        {(["CPU", "GPU"] as const).map((side, i) => {
          const id = `fan${i + 1}` as FanId;
          const temp = i ? live?.gpu_temp : live?.primary_temp;
          const trace = traces.data?.decisions
            .filter((d) => d.fan_id === id)
            .at(-1);
          return (
            <Panel
              key={side}
              title={`${side} cooling`}
              actions={<Link to="/fans">Adjust fan</Link>}
            >
              <Stat
                label={`${side} temperature`}
                value={<TempBadge value={temp} />}
                detail={
                  live?.critical_active
                    ? "Critical protection active"
                    : temp == null
                      ? "Control sensor unavailable"
                      : "Live control sensor"
                }
              />
              <Sparkline
                values={history
                  .slice(-600)
                  .map((p) => (i ? p.gpu_temp : p.primary_temp))}
                label={`${side} temperature over five minutes`}
              />
              <div className="stats">
                <Stat
                  label="Measured duty"
                  value={live?.[id] == null ? "Unavailable" : `${live[id]}%`}
                />
                <Stat
                  label="Tachometer"
                  value={
                    live?.[i ? "rpm2" : "rpm1"] == null
                      ? "Unavailable"
                      : `${live[i ? "rpm2" : "rpm1"]} RPM`
                  }
                />
              </div>
              <p className="muted">
                {trace
                  ? trace.safety_override
                    ? "Critical protection → 100% cooling"
                    : `${trace.sensor_id || "Manual target"} → ${trace.configured_curve || trace.policy_source} → ${trace.final_duty ?? "Unavailable"}%`
                  : "Waiting for the first control decision."}
              </p>
            </Panel>
          );
        })}
      </div>
      <Panel title="Control policy">
        <div className="form-grid">
          <Select
            label="Configured mode"
            value={config.mode}
            disabled={disabled}
            onChange={(mode) =>
              act("mode", { mode: mode === "auto" ? "curve" : mode })
            }
          >
            <option value="auto">Automatic</option>
            <option value="manual">Manual</option>
            <option value="released" disabled>
              EC automatic
            </option>
          </Select>
          <Select
            label="Configured profile"
            value={config.profile}
            disabled={disabled}
            onChange={(profile) => act("profile", { profile })}
          >
            {Object.keys(snapshot?.profiles || {}).map((p) => (
              <option key={p}>{p}</option>
            ))}
          </Select>
        </div>
        <div className="actions">
          <ConfirmDialog
            label="Return to EC Auto"
            title="Return control to firmware?"
            disabled={disabled}
            onConfirm={() => act("mode", { mode: "released" })}
          >
            The embedded controller will manage cooling using its firmware
            policy.
          </ConfirmDialog>
          <span className="muted">
            Effective policy: {live?.policy_source || "Waiting"}
            {live?.active_rules?.length
              ? ` · Active rules: ${live.active_rules.join(", ")}`
              : ""}
          </span>
        </div>
      </Panel>
      <Panel title="Physical fans">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fan</th>
                <th>Duty</th>
                <th>Policy</th>
                <th>Limits</th>
              </tr>
            </thead>
            <tbody>
              {present.map((n) => {
                const id = `fan${n}` as FanId,
                  fan = config.fans[id];
                return (
                  <tr key={id}>
                    <td>
                      <Link to="/fans">{fan.name}</Link>
                    </td>
                    <td>
                      <DutyBar value={live?.[id]} label={id} />
                    </td>
                    <td>
                      {fan.control.type}
                      {fan.control.curve_ref
                        ? ` / ${config.curves[fan.control.curve_ref]?.name || "Missing curve"}`
                        : ""}
                    </td>
                    <td>
                      {fan.min_duty}–{fan.max_duty}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
      {snapshot?.warnings?.length ? (
        <Panel title="Configuration warnings">
          {snapshot.warnings.map((w, i) => (
            <p key={i} className="warning">
              {w}
            </p>
          ))}
        </Panel>
      ) : null}
    </>
  );
}
