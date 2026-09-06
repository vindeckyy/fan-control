import { useState } from "react";
import { client, request, type Fan, type FanId } from "../client";
import {
  Panel,
  Select,
  Slider,
  Toggle,
  TempBadge,
  DutyBar,
} from "../components/ui";
import { mutate, useConfig } from "../stores/configStore";
import { useLive } from "../stores/liveStore";
import { tell } from "../stores/uiStore";
import { Feature, NumberField, useDisabled, useResource } from "./shared";
function FanEditor({ id, fan }: { id: FanId; fan: Fan }) {
  const [draft, setDraft] = useState(fan),
    [testing, setTesting] = useState(false);
  const config = useConfig((s) => s.config)!,
    live = useLive((s) => s.live),
    disabled = useDisabled();
  const traces = useResource(client.decisions, [], 2000),
    trace = traces.data?.decisions.filter((d) => d.fan_id === id).at(-1);
  function change(patch: Partial<Fan>) {
    setDraft({ ...draft, ...patch });
  }
  return (
    <Panel title={fan.name} actions={<span className="muted">{id}</span>}>
      <div className="two-col">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void mutate("fans.configure", { fan: id, ...draft }).catch(
              () => {},
            );
          }}
        >
          <label className="field">
            <span>Fan name</span>
            <input
              required
              maxLength={64}
              value={draft.name}
              onChange={(e) => change({ name: e.target.value })}
            />
          </label>
          <Toggle
            label="Control this fan"
            checked={draft.enabled}
            onChange={(enabled) => change({ enabled })}
          />
          <Select
            label="Control source"
            value={draft.control.type}
            onChange={(type) =>
              change({
                control: {
                  ...draft.control,
                  type: type as Fan["control"]["type"],
                  curve_ref:
                    draft.control.curve_ref || Object.keys(config.curves)[0],
                },
              })
            }
          >
            <option value="linked">Hottest source / profile</option>
            <option value="profile">Side temperature / profile</option>
            <option value="curve">Named curve</option>
            <option value="manual">Manual target</option>
          </Select>
          {draft.control.type === "curve" && (
            <Select
              label="Assigned curve"
              value={draft.control.curve_ref || ""}
              onChange={(curve_ref) =>
                change({ control: { ...draft.control, curve_ref } })
              }
            >
              {Object.entries(config.curves).map(([key, c]) => (
                <option key={key} value={key}>
                  {c.name}
                </option>
              ))}
            </Select>
          )}
          {draft.control.type === "manual" && (
            <Slider
              label="Manual target"
              value={draft.control.target || 0}
              onChange={(target) =>
                change({ control: { ...draft.control, target } })
              }
            />
          )}
          <div className="form-grid">
            <NumberField
              label="Minimum duty (%)"
              value={draft.min_duty}
              min={0}
              max={draft.max_duty}
              onChange={(min_duty) => change({ min_duty })}
            />
            <NumberField
              label="Maximum duty (%)"
              value={draft.max_duty}
              min={draft.min_duty}
              max={100}
              onChange={(max_duty) => change({ max_duty })}
            />
          </div>
          <button
            className="primary"
            disabled={disabled || JSON.stringify(fan) === JSON.stringify(draft)}
          >
            Save fan policy
          </button>
        </form>
        <div>
          <DutyBar value={live?.[id]} label="Measured duty" />
          <p>
            Tachometer: {live?.[`rpm${id.at(-1)}` as "rpm1"] ?? "Unavailable"}{" "}
            RPM
          </p>
          <h3>Latest control decision</h3>
          {trace ? (
            <dl className="trace">
              <dt>Source</dt>
              <dd>
                {trace.sensor_id || "Manual"} ·{" "}
                <TempBadge value={trace.source_temp} />
              </dd>
              <dt>Curve / policy</dt>
              <dd>{trace.configured_curve || trace.policy_source}</dd>
              <dt>Requested</dt>
              <dd>{trace.requested_duty ?? "Unavailable"}%</dd>
              <dt>After limits</dt>
              <dd>{trace.bounded_duty ?? "Unavailable"}%</dd>
              <dt>Final</dt>
              <dd>
                {trace.final_duty ?? "Unavailable"}%{" "}
                {trace.safety_override && (
                  <b className="critical">Critical override</b>
                )}
              </dd>
              <dt>Hardware write</dt>
              <dd>
                {trace.write_performed
                  ? "Written"
                  : trace.write_suppressed_reason || "No write"}
              </dd>
            </dl>
          ) : (
            <p className="muted">Waiting for a control decision.</p>
          )}
          <button
            disabled={disabled || testing}
            onClick={async () => {
              setTesting(true);
              try {
                const reply = await request<{ duty: number }>("fans.test", {
                  fan: id,
                  delta: 10,
                  duration_ms: 5000,
                });
                tell(
                  `${fan.name}: ${reply.duty}% requested for five seconds. Safety still applies.`,
                );
              } catch (e) {
                tell((e as Error).message);
              } finally {
                setTesting(false);
              }
            }}
          >
            Test +10% for 5 seconds
          </button>
          <p className="muted">
            The daemon expires the test even if this window closes. Per-fan
            policies apply in Automatic mode; critical protection overrides
            these limits.
          </p>
        </div>
      </div>
    </Panel>
  );
}
export default function Fans() {
  const config = useConfig((s) => s.config)!,
    fans = useConfig((s) => s.snapshot?.fans || []);
  return (
    <>
      <h1>Fans</h1>
      <Feature name="independent fan policies" feature="per_fan_curves">
        {fans.map((n) => {
          const id = `fan${n}` as FanId;
          return (
            <FanEditor
              key={`${id}:${config.revision}`}
              id={id}
              fan={config.fans[id]}
            />
          );
        })}
      </Feature>
    </>
  );
}
