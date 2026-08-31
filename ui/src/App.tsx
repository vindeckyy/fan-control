import { useEffect, useMemo, useState } from "react";
import { call, onFanEvent } from "./bridge";
import type { CurvePoint, HistoryPoint, Snapshot } from "./types";
import Banners from "./components/Banners";
import CurveStudio from "./components/CurveStudio";
import FanCards from "./components/FanCard";
import Hero from "./components/Hero";
import HistoryBoard from "./components/HistoryBoard";
import SafetyDrawer from "./components/SafetyDrawer";
import SensorRail from "./components/SensorRail";
import Toast from "./components/Toast";

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [toast, setToast] = useState("");
  const [diagnose, setDiagnose] = useState("");
  const [selectedFan, setSelectedFan] = useState(1);

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

  useEffect(() => {
    void run("snapshot").then((value) => value && setSnap(value as Snapshot));
    void run("history").then((value) => {
      const payload = value as { history?: HistoryPoint[] } | null;
      if (payload?.history) setHistory(payload.history);
    });
    return onFanEvent((name, payload) => {
      if (name === "snapshot") setSnap(payload as Snapshot);
      if (name === "history") setHistory((payload as { history: HistoryPoint[] }).history || []);
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
      if (!snap) return;
      const modes: Array<() => void> = [
        () => run("mode", { mode: "manual" }, "Manual"),
        () => run("profile", { profile: "silent" }, "Silent"),
        () => run("profile", { profile: "balanced" }, "Balanced"),
        () => run("profile", { profile: "performance" }, "Performance"),
        () => run("profile", { profile: "custom" }, "Custom"),
        () => run("mode", { mode: "released" }, "EC Auto"),
      ];
      if (event.key >= "1" && event.key <= "6") modes[Number(event.key) - 1]();
      if (event.key === "e" || event.key === "E") run("mode", { mode: "released" }, "EC Auto");
      if (event.key === "l" || event.key === "L") run("config", { linked: !snap.linked }, snap.linked ? "Unlinked" : "Linked");
      if (event.key === "[" || event.key === "]") {
        const delta = event.key === "]" ? 5 : -5;
        const current = Number(snap.targets[String(selectedFan)] ?? 0);
        run("set", { fan: selectedFan, pct: Math.max(0, Math.min(100, current + delta)) });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [snap, selectedFan]);

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
