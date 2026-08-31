import { useEffect, useMemo, useState } from "react";
import type { CurvePoint, Snapshot } from "../types";
import { interpolate, normalizeCurve, pathForCurve } from "../curveMath";

type Which = "shared" | "cpu" | "gpu";

type Props = {
  snap: Snapshot;
  onApply: (curve: CurvePoint[], which: Which) => void;
  onSaveNamed: (name: string, curve: CurvePoint[]) => void;
  onLoadNamed: (name: string) => void;
  onDeleteNamed: (name: string) => void;
  onExportNamed: (name: string) => void;
  onImport: () => void;
};

export default function CurveStudio({
  snap,
  onApply,
  onSaveNamed,
  onLoadNamed,
  onDeleteNamed,
  onExportNamed,
  onImport,
}: Props) {
  const [which, setWhich] = useState<Which>("shared");
  const [points, setPoints] = useState<CurvePoint[]>(snap.custom_curve || snap.curve);
  const [selected, setSelected] = useState(0);
  const [name, setName] = useState("");
  const named = Object.keys(snap.named_curves || {});

  useEffect(() => {
    if (which === "cpu" && snap.curve_cpu) setPoints(snap.curve_cpu);
    else if (which === "gpu" && snap.curve_gpu) setPoints(snap.curve_gpu);
    else setPoints(snap.custom_curve || snap.curve);
  }, [snap.custom_curve, snap.curve, snap.curve_cpu, snap.curve_gpu, which]);

  const width = 1000;
  const height = 240;
  const path = useMemo(() => {
    try {
      return pathForCurve(normalizeCurve(points), width, height);
    } catch {
      return "";
    }
  }, [points]);

  function toPoint(event: React.PointerEvent<SVGSVGElement>): CurvePoint {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    return [Math.round(x * 110), Math.round((1 - y) * 100)];
  }

  function onPointerDown(event: React.PointerEvent<SVGSVGElement>) {
    const [temp, duty] = toPoint(event);
    const hit = points.findIndex(([t, d]) => Math.abs(t - temp) < 4 && Math.abs(d - duty) < 6);
    if (hit >= 0) {
      setSelected(hit);
      event.currentTarget.setPointerCapture(event.pointerId);
      return;
    }
    const next = [...points, [temp, duty] as CurvePoint];
    try {
      setPoints(normalizeCurve(next));
    } catch {
      setPoints(next);
    }
  }

  function onPointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (event.buttons === 0 || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const [temp, duty] = toPoint(event);
    setPoints((current) => current.map((point, i) => (i === selected ? [temp, duty] : point)));
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Delete" && event.key !== "Backspace") return;
      if (points.length <= 2) return;
      setPoints((current) => current.filter((_, i) => i !== selected));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [points.length, selected]);

  const live = snap.control_temp != null && points.length >= 2 ? interpolate(snap.control_temp, points) : null;

  return (
    <section className="panel span2">
      <div className="sectionHead">
        <h2>Curve studio</h2>
        <div className="row">
          {!snap.linked && (
            <div className="tabs">
              {(["shared", "cpu", "gpu"] as Which[]).map((item) => (
                <button key={item} className={which === item ? "iconBtn active" : "iconBtn"} onClick={() => setWhich(item)}>
                  {item}
                </button>
              ))}
            </div>
          )}
          {["silent", "balanced", "performance"].map((profile) => (
            <button key={profile} className="secondary" onClick={() => setPoints(snap.profiles[profile])}>
              Load {profile}
            </button>
          ))}
        </div>
      </div>
      <svg
        className="curveSvg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        aria-label="Fan curve editor"
      >
        <path d={path} fill="none" stroke="var(--blue)" strokeWidth="3" />
        {points.map(([temp, duty], index) => (
          <circle
            key={`${temp}-${index}`}
            className="handle"
            cx={(Math.min(110, temp) / 110) * width}
            cy={height - (duty / 100) * height}
            r={index === selected ? 9 : 7}
          />
        ))}
        {snap.control_temp != null && live != null && (
          <circle cx={(Math.min(110, snap.control_temp) / 110) * width} cy={height - (live / 100) * height} r="6" fill="var(--amber)" />
        )}
      </svg>
      <div className="curveRows">
        {points.map(([temp, duty], index) => (
          <div className="curveRow" key={index}>
            <input
              type="number"
              min={0}
              max={150}
              value={temp}
              aria-label={`Temperature point ${index + 1}`}
              onChange={(e) => setPoints((current) => current.map((p, i) => (i === index ? [Number(e.target.value), p[1]] : p)))}
            />
            <span>°C →</span>
            <input
              type="number"
              min={0}
              max={100}
              value={duty}
              aria-label={`Duty point ${index + 1}`}
              onChange={(e) => setPoints((current) => current.map((p, i) => (i === index ? [p[0], Number(e.target.value)] : p)))}
            />
            <button className="iconBtn danger" onClick={() => points.length > 2 && setPoints(points.filter((_, i) => i !== index))}>
              ×
            </button>
          </div>
        ))}
      </div>
      <div className="actions">
        <button className="secondary" onClick={() => setPoints([...points, [Math.min(150, (points.at(-1)?.[0] ?? 60) + 5), Math.min(100, (points.at(-1)?.[1] ?? 50) + 10)]])}>
          Add point
        </button>
        <button className="primary" onClick={() => onApply(normalizeCurve(points), which)}>
          Apply curve
        </button>
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 120 }} />
        <button className="secondary" onClick={() => name && onSaveNamed(name, normalizeCurve(points))}>
          Save named
        </button>
        <button className="secondary" onClick={onImport}>
          Import
        </button>
      </div>
      {named.length > 0 && (
        <div className="row" style={{ marginTop: 10 }}>
          {named.map((item) => (
            <span className="row" key={item}>
              <button className="secondary" onClick={() => onLoadNamed(item)}>{item}</button>
              <button className="iconBtn" onClick={() => onExportNamed(item)}>↓</button>
              <button className="iconBtn danger" onClick={() => onDeleteNamed(item)}>×</button>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
