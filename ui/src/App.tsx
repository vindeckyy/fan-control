import { useEffect, useMemo, useRef, useState } from "react";
import { call, onFanEvent } from "./bridge";
import type { HistoryPoint, Snapshot } from "./types";
import { applyLive } from "./liveState";
import Banners from "./components/Banners";
import CurveStudio from "./components/CurveStudio";
import FanCards from "./components/FanCard";
import Hero from "./components/Hero";
import HistoryBoard from "./components/HistoryBoard";
import SafetyDrawer from "./components/SafetyDrawer";
import SensorRail from "./components/SensorRail";
import Toast from "./components/Toast";

const HISTORY_CAP = 900;

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [toast, setToast] = useState("");
  const [diagnose, setDiagnose] = useState("");
  const [selectedFan, setSelectedFan] = useState(1);
  const snapRef = useRef(snap);
  const selectedRef = useRef(selectedFan);
  snapRef.current = snap;
  selectedRef.current = selectedFan;

  function note(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 2200);
  }

  async function run(method: string, params: Record<string, unknown> = {}, message?: string) {
    try {
      const result = await call(method, params);
      if (message) note(message);
      return result;
    } catch (error) {
      note(error instanceof Error ? error.message : String(error));
      return null;
    }
  }

  const runRef = useRef(run);
  runRef.current = run;

  useEffect(() => {
    void run("snapshot").then((value) => value && setSnap(value as Snapshot));
    void run("history").then((value) => {
      const payload = value as { history?: HistoryPoint[] } | null;
      if (payload?.history) setHistory(payload.history);
    });
    return onFanEvent((name, payload) => {
      if (name === "snapshot") setSnap(payload as Snapshot);
      if (name === "live") setSnap((current) => (current ? applyLive(current, payload as Snapshot) : current));
      if (name === "history") setHistory((payload as { history: HistoryPoint[] }).history || []);
      if (name === "history-append") {
        const point = payload as HistoryPoint;
        if (!point || typeof point.time !== "number") return;
        setHistory((current) => {
          const last = current[current.length - 1];
          if (last && last.time === point.time) {
            return current.slice(0, -1).concat(point).slice(-HISTORY_CAP);
          }
          return current.concat(point).slice(-HISTORY_CAP);
        });
      }
      if (name === "toast") note((payload as { message: string }).message);
    });
  }, []);

  useEffect(() => {
    const theme = snap?.theme || localStorage.getItem("fan-theme") || "dark";
    document.documentElement.dataset.theme = theme;
    if (window.matchMedia("(prefers-color-scheme: light)").matches && !snap?.theme && !localStorage.getItem("fan-theme")) {
      document.documentElement.dataset.theme = "light";
    }
  }, [snap?.theme]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const current = snapRef.current;
      if (!current) return;
      const runNow = runRef.current;
      const fan = selectedRef.current;
      const modes: Array<() => void> = [
        () => runNow("mode", { mode: "manual" }, "Manual"),
        () => runNow("profile", { profile: "silent" }, "Silent"),
        () => runNow("profile", { profile: "balanced" }, "Balanced"),
        () => runNow("profile", { profile: "performance" }, "Performance"),
        () => runNow("profile", { profile: "custom" }, "Custom"),
        () => runNow("mode", { mode: "released" }, "EC Auto"),
      ];
      if (event.key >= "1" && event.key <= "6") modes[Number(event.key) - 1]();
      if (event.key === "e" || event.key === "E") runNow("mode", { mode: "released" }, "EC Auto");
      if (event.key === "l" || event.key === "L") runNow("config", { linked: !current.linked }, current.linked ? "Unlinked" : "Linked");
      if (event.key === "[" || event.key === "]") {
        const delta = event.key === "]" ? 5 : -5;
        const value = Number(current.targets[String(fan)] ?? 0);
        runNow("set", { fan, pct: Math.max(0, Math.min(100, value + delta)) });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const spark = useMemo(
    () => history.filter((p) => p.time >= Math.floor(Date.now() / 1000) - 60).map((p) => p.control_temp),
    [history],
  );

  if (!snap) {
    return (
      <main className="shell">
        <Banners snap={null} />
        <Toast message={toast} />
      </main>
    );
  }

  return (
    <main className="shell">
      <Banners snap={snap} />
      <div className="grid">
        <div>
          <Hero
            snap={snap}
            spark={spark}
            onMode={(mode) => run("mode", { mode }, mode)}
            onProfile={(profile) => run("profile", { profile }, profile)}
          />
          <FanCards
            snap={snap}
            selected={selectedFan}
            onSelect={setSelectedFan}
            onSet={(fan, pct) => run("set", { fan, pct })}
            onLinked={(linked) => run("config", { linked }, linked ? "Fans linked" : "Fans unlinked")}
          />
        </div>
        <SensorRail
          snap={snap}
          onPin={(kind, pin) => run("config", { [kind]: pin }, pin ? `Pinned ${kind}` : "Pin cleared")}
        />
        <HistoryBoard history={history} snap={snap} onExport={() => run("history.pick_export")} />
        <CurveStudio
          snap={snap}
          onApply={(curve, which) => run("custom", { curve, which }, "Custom curve active")}
          onSaveNamed={(name, curve) => run("curves.save", { name, curve }, `Saved ${name}`)}
          onLoadNamed={(name) => run("curves.load", { name }, `Loaded ${name}`)}
          onDeleteNamed={(name) => run("curves.delete", { name }, `Deleted ${name}`)}
          onExportNamed={(name) => run("curves.pick_export", { name })}
          onImport={() => run("curves.pick_import")}
        />
        <SafetyDrawer
          snap={snap}
          diagnose={diagnose}
          onConfig={(patch) => run("config", patch, "Setting saved")}
          onDiagnose={async () => {
            const result = (await run("diagnose")) as { text?: string } | null;
            if (result?.text) setDiagnose(result.text);
          }}
          onEcAuto={() => run("mode", { mode: "released" }, "EC Auto")}
          onTheme={() => {
            const theme = snap.theme === "dark" ? "light" : "dark";
            localStorage.setItem("fan-theme", theme);
            run("config", { theme }, `${theme} theme`);
          }}
        />
      </div>
      <Toast message={toast} />
    </main>
  );
}
