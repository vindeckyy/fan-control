import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import type { HistoryPoint, Snapshot } from "../types";
import { interpolate } from "../curveMath";
import { windowedHistory } from "../liveState";

type Props = {
  history: HistoryPoint[];
  snap: Snapshot;
  onExport: () => void;
};

const WINDOWS = [5, 15, 30];

export default function HistoryBoard({ history, snap, onExport }: Props) {
  const [tab, setTab] = useState<"history" | "curve">("history");
  const [windowMin, setWindowMin] = useState(30);
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const paused = useRef(false);
  const readout = useRef<HTMLDivElement>(null);
  const critical = useRef(snap.critical_temp);
  critical.current = snap.critical_temp;

  const nowSec = history.length ? history[history.length - 1].time : Math.floor(Date.now() / 1000);
  const rows = useMemo(() => windowedHistory(history, windowMin, nowSec), [history, windowMin, nowSec]);
  const linked = snap.linked !== false;

  useEffect(() => {
    if (tab !== "history" || !host.current) return;
    const series: uPlot.Series[] = [
      {},
      { label: "CPU", scale: "C", stroke: "#ffbd5b", width: 2 },
      { label: "GPU", scale: "C", stroke: "#57dfcf", width: 2 },
      { label: "Fan", scale: "%", stroke: "#62a8ff", width: 2 },
    ];
    if (!linked) {
      series.push({ label: "Fan 2", scale: "%", stroke: "#9ec5ff", width: 2 });
    }
    const opts: uPlot.Options = {
      width: host.current.clientWidth || 800,
      height: 220,
      legend: { show: false },
      scales: {
        x: { time: true, auto: false },
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
            const idx = u.cursor.idx;
            paused.current = idx != null;
            const el = readout.current;
            if (!el) return;
            if (idx == null) {
              el.textContent = "Hover a point for values";
              return;
            }
            const time = u.data[0][idx];
            const cpu = u.data[1][idx];
            const gpu = u.data[2][idx];
            const fan = u.data[3][idx];
            const when = typeof time === "number" ? new Date(time * 1000).toLocaleTimeString() : "--";
            const fmt = (value: number | null | undefined) => (value == null ? "--" : String(Math.round(value * 10) / 10));
            el.textContent = `${when}  CPU ${fmt(cpu)}°C  GPU ${fmt(gpu)}°C  Fan ${fmt(fan)}%`;
          },
        ],
        draw: [
          (u) => {
            const y = u.valToPos(critical.current, "C", true);
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
    const instance = new uPlot(opts, [[], [], [], []], host.current);
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
  }, [tab, linked, rows.length >= 2]);

  useEffect(() => {
    if (!plot.current || paused.current || tab !== "history") return;
    const end = nowSec;
    const start = end - windowMin * 60;
    const data: uPlot.AlignedData = [
      rows.map((p) => p.time),
      rows.map((p) => p.temp),
      rows.map((p) => p.gpu_temp),
      rows.map((p) => p.fan1),
    ];
    if (!linked) data.push(rows.map((p) => p.fan2));
    plot.current.setData(data, false);
    plot.current.setScale("x", { min: start, max: end });
  }, [rows, tab, linked, windowMin, nowSec]);

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
            <span><i style={{ background: "var(--red)" }} />Critical {snap.critical_temp}°C</span>
          </div>
          {rows.length < 2 ? <div className="empty">Collecting history…</div> : <div className="chartWrap" ref={host} />}
          <div className="chartReadout" ref={readout}>Hover a point for values</div>
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
