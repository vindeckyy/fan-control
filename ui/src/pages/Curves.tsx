import { useRef, useState, type PointerEvent } from "react";
import { type Curve, type FanId, request } from "../client";
import { ConfirmDialog, Panel, Select, Toggle } from "../components/ui";
import {
  deletePoint,
  insertPoint,
  interpolate,
  movePoint,
  pathForCurve,
  validateCurve,
} from "../curveMath";
import { act, mutate, useConfig } from "../stores/configStore";
import { useLive } from "../stores/liveStore";
import { tell, useUI } from "../stores/uiStore";
import { Feature, useDisabled } from "./shared";
function Editor({ id, curve }: { id: string; curve: Curve }) {
  const [draft, setDraft] = useState(curve),
    [overlays, setOverlays] = useState<string[]>([]);
  const svg = useRef<SVGSVGElement>(null),
    dragging = useRef<number | null>(null);
  const config = useConfig((s) => s.config)!,
    fans = useConfig((s) => s.snapshot?.fans || []),
    live = useLive((s) => s.live),
    disabled = useDisabled();
  const temp =
    draft.temp_source === "cpu"
      ? live?.primary_temp
      : draft.temp_source === "gpu"
        ? live?.gpu_temp
        : draft.temp_source === "max"
          ? live?.control_temp
          : live?.temps?.find(
              (s) => (s as unknown as { id: string }).id === draft.temp_source,
            )?.temp;
  const dirty = JSON.stringify(curve) !== JSON.stringify(draft),
    error = validateCurve(draft.points);
  function drag(e: PointerEvent<SVGSVGElement>) {
    if (dragging.current === null || !svg.current) return;
    const rect = svg.current.getBoundingClientRect();
    setDraft((d) => ({
      ...d,
      points: movePoint(
        d.points,
        dragging.current!,
        ((e.clientX - rect.left) / rect.width) * 150,
        100 - ((e.clientY - rect.top) / rect.height) * 100,
      ),
    }));
  }
  const assigned = fans.filter(
    (n) => config.fans[`fan${n}` as FanId].control.curve_ref === id,
  );
  return (
    <Panel
      title="Curve Studio"
      actions={
        <span className="muted">
          {dirty ? "Unsaved preview" : "Saved curve"} · Celsius policy units
        </span>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void mutate("curves.set", { id, ...draft }).catch(() => {});
        }}
      >
        <div className="form-grid">
          <label className="field">
            <span>Curve name</span>
            <input
              required
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          <Select
            label="Temperature source"
            value={draft.temp_source}
            onChange={(temp_source) => setDraft({ ...draft, temp_source })}
          >
            <option value="cpu">CPU alias</option>
            <option value="gpu">GPU alias</option>
            <option value="max">Hottest CPU / GPU</option>
            {live?.temps?.map((s, i) => {
              const record = s as unknown as { id?: string };
              return (
                record.id && (
                  <option key={i} value={record.id}>
                    {s.name}: {s.label}
                  </option>
                )
              );
            })}
          </Select>
        </div>
        <div className="curve-frame">
          <span className="muted">100% duty</span>
          <svg
            ref={svg}
            className="curve-editor"
            viewBox="0 0 600 240"
            preserveAspectRatio="none"
            role="group"
            aria-label="Curve points, use arrow keys to adjust"
            onPointerMove={drag}
            onPointerUp={() => {
              dragging.current = null;
            }}
            onPointerCancel={() => {
              dragging.current = null;
            }}
          >
            {[0, 25, 50, 75, 100].map((v) => (
              <line
                key={v}
                x1={0}
                x2={600}
                y1={240 - v * 2.4}
                y2={240 - v * 2.4}
                stroke="var(--border-subtle)"
              />
            ))}
            {overlays.map((key) => (
              <path
                key={key}
                d={pathForCurve(config.curves[key].points, 600, 240, 150)}
                fill="none"
                stroke="var(--text-secondary)"
                strokeWidth={1.5}
              />
            ))}
            <path
              d={pathForCurve(curve.points, 600, 240, 150)}
              fill="none"
              stroke="var(--text-secondary)"
              strokeWidth={2}
            />
            <path
              d={pathForCurve(draft.points, 600, 240, 150)}
              fill="none"
              stroke="var(--reading-normal)"
              strokeWidth={3}
              strokeDasharray={dirty ? "6 4" : undefined}
            />
            {draft.points.map(([t, d], i) => (
              <circle
                key={i}
                cx={t * 4}
                cy={240 - d * 2.4}
                r={6}
                fill="var(--surface-card)"
                stroke="var(--reading-normal)"
                strokeWidth={3}
                tabIndex={0}
                role="button"
                aria-label={`Point ${i + 1}: ${t}°C, ${d}% duty. Arrow keys adjust; Delete removes.`}
                onPointerDown={(e) => {
                  dragging.current = i;
                  svg.current?.setPointerCapture(e.pointerId);
                }}
                onKeyDown={(e) => {
                  if (
                    [
                      "ArrowLeft",
                      "ArrowRight",
                      "ArrowUp",
                      "ArrowDown",
                      "Delete",
                    ].includes(e.key)
                  ) {
                    e.preventDefault();
                    setDraft({
                      ...draft,
                      points:
                        e.key === "Delete"
                          ? deletePoint(draft.points, i)
                          : movePoint(
                              draft.points,
                              i,
                              t +
                                (e.key === "ArrowRight"
                                  ? 1
                                  : e.key === "ArrowLeft"
                                    ? -1
                                    : 0),
                              d +
                                (e.key === "ArrowUp"
                                  ? 1
                                  : e.key === "ArrowDown"
                                    ? -1
                                    : 0),
                            ),
                    });
                  }
                }}
              />
            ))}
          </svg>
          <div className="axis-labels">
            <span>0°C / 0%</span>
            <span>50°C</span>
            <span>100°C</span>
            <span>150°C</span>
          </div>
        </div>
        <p>
          {temp == null
            ? "Waiting for the selected temperature source."
            : `At ${temp.toFixed(1)}°C: saved ${Math.round(interpolate(temp, curve.points))}% → preview ${Math.round(interpolate(temp, draft.points))}%`}
          . Preview excludes safety, limits, and automation.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Point</th>
                <th>Temperature (°C)</th>
                <th>Duty (%)</th>
                <th>Remove</th>
              </tr>
            </thead>
            <tbody>
              {draft.points.map(([t, d], i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>
                    <input
                      aria-label={`Point ${i + 1} temperature`}
                      type="number"
                      value={t}
                      min={0}
                      max={150}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          points: movePoint(
                            draft.points,
                            i,
                            +e.target.value,
                            d,
                          ),
                        })
                      }
                    />
                  </td>
                  <td>
                    <input
                      aria-label={`Point ${i + 1} duty`}
                      type="number"
                      value={d}
                      min={0}
                      max={100}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          points: movePoint(
                            draft.points,
                            i,
                            t,
                            +e.target.value,
                          ),
                        })
                      }
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      disabled={draft.points.length <= 2}
                      onClick={() =>
                        setDraft({
                          ...draft,
                          points: deletePoint(draft.points, i),
                        })
                      }
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {error && <p role="alert">{error}</p>}
        <div className="actions curve-actions">
          <button
            type="button"
            onClick={() => {
              const pair = draft.points
                .slice(1)
                .map((p, i) => ({
                  gap: p[0] - draft.points[i][0],
                  t: Math.round((p[0] + draft.points[i][0]) / 2),
                }))
                .sort((a, b) => b.gap - a.gap)[0];
              if (pair.gap > 1)
                setDraft({
                  ...draft,
                  points: insertPoint(draft.points, pair.t),
                });
            }}
          >
            Insert midpoint
          </button>
          <button
            type="button"
            disabled={!dirty}
            onClick={() => setDraft(curve)}
          >
            Discard preview
          </button>
          <button className="primary" disabled={disabled || !!error || !dirty}>
            Save curve
          </button>
        </div>
      </form>
      <hr />
      <h3>Assignments</h3>
      <div className="actions">
        {fans.map((n) => {
          const fan = config.fans[`fan${n}` as FanId];
          return (
            <button
              key={n}
              disabled={disabled || assigned.includes(n)}
              onClick={() => act("curves.assign", { fan: n, curve_ref: id })}
            >
              {assigned.includes(n)
                ? `${fan.name}: assigned`
                : `Assign ${fan.name}`}
            </button>
          );
        })}
      </div>
      <h3 className="subheading">Compare saved curves</h3>
      {Object.entries(config.curves)
        .filter(([key]) => key !== id)
        .map(([key, c]) => (
          <Toggle
            key={key}
            label={c.name}
            checked={overlays.includes(key)}
            onChange={(on) =>
              setOverlays(
                on ? [...overlays, key] : overlays.filter((k) => k !== key),
              )
            }
          />
        ))}
      <div className="actions">
        <button
          disabled={disabled}
          onClick={() =>
            act("curves.set", { ...draft, name: `${draft.name} copy` })
          }
        >
          Duplicate curve
        </button>
        <button
          onClick={() => {
            void request("curves.pick_export", { id, name: curve.name }).catch(
              (e) => tell(e.message),
            );
          }}
        >
          Export JSON
        </button>
        <ConfirmDialog
          label="Delete curve"
          title="Delete this curve?"
          disabled={disabled}
          onConfirm={() => act("curves.delete", { id, force: true })}
        >
          {assigned.length
            ? "Assigned fans will return to their side-temperature profile. This removes the saved curve."
            : "This removes the saved curve."}
        </ConfirmDialog>
      </div>
    </Panel>
  );
}
export default function Curves() {
  const config = useConfig((s) => s.config)!,
    selected = useUI((s) => s.selectedCurve),
    disabled = useDisabled();
  const id =
    selected && config.curves[selected]
      ? selected
      : Object.keys(config.curves)[0];
  return (
    <>
      <h1>Curves</h1>
      <Feature feature="per_fan_curves" name="Curve Studio">
        <div className="split">
          <div>
            <Panel title="Saved curves">
              <div className="list">
                {Object.entries(config.curves).map(([key, c]) => (
                  <button
                    key={key}
                    className={key === id ? "selected" : ""}
                    onClick={() => useUI.setState({ selectedCurve: key })}
                  >
                    {c.name}
                    <br />
                    <small>
                      {c.temp_source} · {c.points.length} points
                    </small>
                  </button>
                ))}
              </div>
            </Panel>
            <div className="list">
              <button
                disabled={disabled}
                onClick={() =>
                  act("curves.set", {
                    name: "New curve",
                    temp_source: "max",
                    points: [
                      [35, 20],
                      [60, 50],
                      [90, 100],
                    ],
                  })
                }
              >
                Create curve
              </button>
              <button
                onClick={() => {
                  void request("curves.pick_import").catch((e) =>
                    tell(e.message),
                  );
                }}
              >
                Import JSON
              </button>
            </div>
          </div>
          {id && (
            <Editor
              key={`${id}:${config.revision}`}
              id={id}
              curve={config.curves[id]}
            />
          )}
        </div>
      </Feature>
    </>
  );
}
