import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import type { HistoryPoint, Snapshot } from "../types";
import { interpolate } from "../curveMath";

type Props = {
  history: HistoryPoint[];
  snap: Snapshot;
  onExport: () => void;
};

const WINDOWS = [5, 15, 30];

export default function HistoryBoard({ history, snap, onExport }: Props) {
  const [tab, setTab] = useState<"history" | "curve">("history");
  const [windowMin, setWindowMin] = useState(30);
  const [paused, setPaused] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);

  const hasRpm = history.some((point) => point.rpm1);
  const cutoff = Math.floor(Date.now() / 1000) - windowMin * 60;
  const rows = history.filter((p) => p.time >= cutoff);

  useEffect(() => {
    if (tab !== "history" || !host.current) return;
    const series: uPlot.Series[] = [
      {},
      { label: "CPU", scale: "C", stroke: "#ffbd5b", width: 2 },
      { label: "GPU", scale: "C", stroke: "#57dfcf", width: 2 },
      { label: "Control", scale: "C", stroke: "#ffbd5b", width: 1, dash: [6, 4] },
      { label: "Fan 1", scale: "%", stroke: "#62a8ff", width: 2 },
      { label: "Fan 2", scale: "%", stroke: "#9ec5ff", width: 2 },
    ];
    if (hasRpm) {
      series.push({ label: "RPM 1", scale: "rpm", stroke: "#91a0b7", width: 1 });
    }
    const opts: uPlot.Options = {
      width: host.current.clientWidth || 800,
      height: 220,
      scales: {
        C: { range: [0, 110] },
        "%": { range: [0, 100] },
      },
      axes: [
        { stroke: "#91a0b7", grid: { stroke: "#263449" } },
        { scale: "C", stroke: "#ffbd5b", grid: { stroke: "#263449" } },
        { scale: "%", side: 1, stroke: "#62a8ff", grid: { show: false } },
      ],
      series,
      cursor: { lock: false },
      hooks: {
        setCursor: [
          (u) => {
            setPaused(u.cursor.idx != null);
          },
        ],
        draw: [
          (u) => {
            const y = u.valToPos(snap.critical_temp, "C", true);
            u.ctx.save();
            u.ctx.strokeStyle = "#ff657a";
            u.ctx.setLineDash([4, 4]);
            u.ctx.beginPath();
            u.ctx.moveTo(u.bbox.left, y);
            u.ctx.lineTo(u.bbox.left + u.bbox.width, y);
            u.ctx.stroke();
            u.ctx.restore();
          },
        ],
      },
    };
    const instance = new uPlot(opts, [[], [], [], [], [], []], host.current);
    plot.current = instance;
    const ro = new ResizeObserver(() => {
      instance.setSize({ width: host.current?.clientWidth || 800, height: 220 });
    });
    ro.observe(host.current);
    return () => {
      ro.disconnect();
      instance.destroy();
      plot.current = null;
    };
  }, [tab, snap.critical_temp, hasRpm]);

  useEffect(() => {
    if (!plot.current || paused || tab !== "history") return;
    const hasRpm = plot.current.series.length > 6;
    const data: uPlot.AlignedData = [
      rows.map((p) => p.time),
      rows.map((p) => p.temp),
      rows.map((p) => p.gpu_temp),
      rows.map((p) => p.control_temp),
      rows.map((p) => p.fan1),
      rows.map((p) => p.fan2),
    ];
    if (hasRpm) data.push(rows.map((p) => p.rpm1));
    plot.current.setData(data);
  }, [rows, paused, tab]);

  const curve = snap.profile === "custom" || snap.mode === "curve" ? snap.custom_curve || snap.curve : snap.curve;
  const cpuCurve = !snap.linked && snap.curve_cpu ? snap.curve_cpu : curve;
  const gpuCurve = !snap.linked && snap.curve_gpu ? snap.curve_gpu : curve;

  return (
    <section className="panel span2">
      <div className="sectionHead">
        <h2>{tab === "history" ? "Temperature history" : "Active curve"}</h2>
        <div className="row">
          <div className="tabs">
            <button className={tab === "history" ? "iconBtn active" : "iconBtn"} onClick={() => setTab("history")}>
              History
            </button>
            <button className={tab === "curve" ? "iconBtn active" : "iconBtn"} onClick={() => setTab("curve")}>
              Curve
            </button>
          </div>
          {tab === "history" && (
            <>
              <div className="windows">
                {WINDOWS.map((mins) => (
                  <button key={mins} className={windowMin === mins ? "iconBtn active" : "iconBtn"} onClick={() => setWindowMin(mins)}>
                    {mins}m
                  </button>
                ))}
              </div>
              <button className="secondary" onClick={onExport}>
                Export CSV
              </button>
            </>
          )}
        </div>
      </div>
      {tab === "history" && (
        <>
          <div className="legend">
            <span><i style={{ background: "var(--amber)" }} />CPU</span>
            <span><i style={{ background: "var(--cyan)" }} />GPU</span>
            <span><i style={{ background: "var(--blue)" }} />Fan duty</span>
            <span><i style={{ background: "var(--red)" }} />Critical</span>
          </div>
          {rows.length < 2 ? <div className="empty">Collecting history…</div> : <div className="chartWrap" ref={host} />}
        </>
      )}
      {tab === "curve" && (
        <CurvePreview
          cpu={cpuCurve}
          gpu={gpuCurve}
          linked={snap.linked}
          temp={snap.control_temp}
          duty={snap.fan1}
        />
      )}
    </section>
  );
}

function CurvePreview({
  cpu,
  gpu,
  linked,
  temp,
  duty,
}: {
  cpu: [number, number][];
  gpu: [number, number][];
  linked: boolean;
  temp: number | null;
  duty: number;
}) {
  const width = 1000;
  const height = 220;
  const toX = (t: number) => (Math.min(110, t) / 110) * width;
  const toY = (d: number) => height - (d / 100) * height;
  const path = (curve: [number, number][]) =>
    curve.map(([t, d], i) => `${i ? "L" : "M"}${toX(t).toFixed(1)},${toY(d).toFixed(1)}`).join(" ");
  const liveDuty = temp != null && cpu.length >= 2 ? interpolate(temp, cpu) : duty;
  return (
    <svg className="curveSvg" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-label="Active fan curve">
      <path d={path(cpu)} fill="none" stroke="var(--blue)" strokeWidth="3" />
      {!linked && <path d={path(gpu)} fill="none" stroke="var(--cyan)" strokeWidth="3" />}
      {temp != null && <circle cx={toX(temp)} cy={toY(liveDuty)} r="7" fill="var(--amber)" />}
    </svg>
  );
}
