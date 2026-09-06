import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import { client, request, type FanId, type History } from "../client";
import { Panel, Select, Stat, TempBadge, EmptyState } from "../components/ui";
import { useLive } from "../stores/liveStore";
import { useConfig } from "../stores/configStore";
import { tell } from "../stores/uiStore";
import { toDisplay } from "../temperature";
import { Feature, ResourceState, useResource } from "./shared";
export function HistoryChart({ data }: { data: History }) {
  const host = useRef<HTMLDivElement>(null),
    unit = useConfig((s) => s.config?.display.temperature_unit || "c"),
    theme = useConfig((s) => s.config?.display.theme);
  useEffect(() => {
    if (!host.current || !data.samples.length) return;
    const style = getComputedStyle(document.documentElement),
      text = style.getPropertyValue("--text-secondary").trim(),
      accent = style.getPropertyValue("--reading-normal").trim();
    const times = [
      ...new Set([
        ...data.samples.map((p) => p.ts),
        ...data.fans.map((p) => p.ts),
      ]),
    ].sort((a, b) => a - b);
    const temperature = new Map(
      data.samples.map((p) => [
        p.ts,
        p.hottest_temp == null ? null : toDisplay(p.hottest_temp, unit),
      ]),
    );
    const ids = [...new Set(data.fans.map((f) => f.fan_id))];
    const series: uPlot.Series[] = [
      {},
      {
        label: `Hottest °${unit.toUpperCase()}`,
        stroke: accent,
        width: 2,
        spanGaps: true,
        scale: "temp",
      },
      ...ids.map((id, i) => ({
        label: `${id} duty %`,
        stroke: text,
        dash: i ? [3, 4] : [8, 4],
        scale: "duty",
        spanGaps: true,
      })),
    ];
    const values: uPlot.AlignedData = [
      times,
      times.map((t) => temperature.get(t) ?? null),
      ...ids.map((id) => {
        const map = new Map(
          data.fans.filter((f) => f.fan_id === id).map((f) => [f.ts, f.duty]),
        );
        return times.map((t) => map.get(t) ?? null);
      }),
    ];
    const plot = new uPlot(
      {
        width: Math.max(240, host.current.clientWidth),
        height: 300,
        series,
        scales: { x: { time: true }, duty: { range: [0, 100] } },
        axes: [
          { stroke: text },
          { scale: "temp", stroke: text },
          { scale: "duty", side: 1, stroke: text },
        ],
        legend: { show: true },
      },
      values,
      host.current,
    );
    const observer = new ResizeObserver(() => {
      if (host.current)
        plot.setSize({
          width: Math.max(240, host.current.clientWidth),
          height: 300,
        });
    });
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      plot.destroy();
    };
  }, [data, unit, theme]);
  return (
    <div
      className="history-chart"
      ref={host}
      role="img"
      aria-label="Temperature and fan duty over time. Exact samples are listed below."
    />
  );
}
function AnalyticsBody() {
  const [range, setRange] = useState("live"),
    history = useLive((s) => s.history),
    presentFans = useConfig((s) => s.snapshot?.fans);
  const resource = useResource(
    async () => {
      const since = Date.now() / 1000 - Number(range === "live" ? 1800 : range);
      const data = await client.history({ since, max_points: 600 });
      const stats = await request<{
        samples: number;
        avg_temp: number | null;
        max_temp: number | null;
        critical_events: number;
        profiles: { profile: string; samples: number }[];
        per_fan: {
          fan_id: string;
          avg_duty: number;
          max_duty: number;
          avg_rpm: number | null;
        }[];
        duty_bands: { fan_id: string; band: string; samples: number }[];
      }>("history.stats", { since });
      return { data, stats };
    },
    [range],
    range === "live" ? 10000 : 0,
  );
  const data: History =
    range === "live"
      ? {
          samples: history.map((p) => ({
            ts: p.timestamp,
            hottest_temp: p.control_temp ?? null,
            critical: !!p.critical_active,
            effective_profile: p.effective_profile,
          })),
          fans: history.flatMap((p) =>
            (presentFans || [])
              .map((n) => `fan${n}` as FanId)
              .filter((id) => p[id] != null)
              .map((id) => ({
                ts: p.timestamp,
                fan_id: id,
                duty: p[id]!,
                rpm: p[`rpm${id.at(-1)}` as "rpm1"] ?? null,
              })),
          ),
          sensors: [],
          degraded: resource.data?.data.degraded || false,
        }
      : resource.data?.data || {
          samples: [],
          fans: [],
          sensors: [],
          degraded: false,
        };
  return (
    <>
      <Panel
        title="Temperature and fan response"
        actions={
          <button
            onClick={() => {
              void request("history.pick_export").catch((e) => tell(e.message));
            }}
          >
            Export CSV
          </button>
        }
      >
        <Select label="Time range" value={range} onChange={setRange}>
          <option value="live">Live / last 30 minutes</option>
          <option value="3600">Last hour</option>
          <option value="86400">Last 24 hours</option>
          <option value="604800">Last 7 days</option>
        </Select>
        {data.degraded && (
          <p className="warning">
            History persistence is degraded. Available memory samples remain
            visible.
          </p>
        )}
        {data.samples.length ? (
          <HistoryChart data={data} />
        ) : (
          <EmptyState>
            Collecting telemetry. Leave Fan Control connected to see temperature
            and fan response.
          </EmptyState>
        )}
      </Panel>
      <ResourceState resource={resource}>
        <Panel title="Selected range">
          <div className="stats">
            <Stat
              label="Recorded samples"
              value={resource.data?.stats.samples ?? 0}
            />
            <Stat
              label="Average temperature"
              value={<TempBadge value={resource.data?.stats.avg_temp} />}
            />
            <Stat
              label="Peak temperature"
              value={<TempBadge value={resource.data?.stats.max_temp} />}
            />
            <Stat
              label="Critical samples"
              value={resource.data?.stats.critical_events ?? 0}
            />
          </div>
          <p className="muted">
            Profile occupancy:{" "}
            {resource.data?.stats.profiles
              .map((p) => `${p.profile}: ${p.samples} samples`)
              .join(" · ") || "No samples in this range."}
          </p>
        </Panel>
      </ResourceState>
      <Panel title="Fan statistics">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fan</th>
                <th>Average duty</th>
                <th>Peak duty</th>
                <th>Average RPM</th>
                <th>Duty distribution</th>
              </tr>
            </thead>
            <tbody>
              {resource.data?.stats.per_fan?.map((fan) => (
                <tr key={fan.fan_id}>
                  <td>{fan.fan_id}</td>
                  <td>{fan.avg_duty.toFixed(1)}%</td>
                  <td>{fan.max_duty}%</td>
                  <td>
                    {fan.avg_rpm == null
                      ? "Unavailable"
                      : Math.round(fan.avg_rpm)}
                  </td>
                  <td>
                    {resource.data?.stats.duty_bands
                      ?.filter((b) => b.fan_id === fan.fan_id)
                      .map((b) => `${b.band}%: ${b.samples} samples`)
                      .join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Recent samples">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Hottest</th>
                <th>Profile</th>
                <th>Thermal state</th>
              </tr>
            </thead>
            <tbody>
              {data.samples
                .slice(-10)
                .reverse()
                .map((p, i) => (
                  <tr key={i}>
                    <td>{new Date(p.ts * 1000).toLocaleString()}</td>
                    <td>
                      <TempBadge value={p.hottest_temp} />
                    </td>
                    <td>{p.effective_profile}</td>
                    <td>{p.critical ? "Critical" : "Normal"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
export default function Analytics() {
  return (
    <>
      <h1>Analytics</h1>
      <Feature name="Analytics" feature="history">
        <AnalyticsBody />
      </Feature>
    </>
  );
}
