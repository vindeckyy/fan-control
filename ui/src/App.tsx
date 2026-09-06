import { useEffect } from "react";
import { HashRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { connect } from "./stores/connect";
import { useConfig } from "./stores/configStore";
import { useConnection } from "./stores/connectionStore";
import { useLive } from "./stores/liveStore";
import { toggleSidebar, useUI } from "./stores/uiStore";
import { TempBadge, Skeleton } from "./components/ui";
import Overview from "./pages/Overview";
import Fans from "./pages/Fans";
import Curves from "./pages/Curves";
import Sensors from "./pages/Sensors";
import Analytics from "./pages/Analytics";
import Automation from "./pages/Automation";
import Settings from "./pages/Settings";
import Diagnostics from "./pages/Diagnostics";
import CommandPalette from "./components/CommandPalette";
export const pages = [
  "Overview",
  "Fans",
  "Curves",
  "Sensors",
  "Analytics",
  "Automation",
  "Settings",
  "Diagnostics",
];
const views = [
  Overview,
  Fans,
  Curves,
  Sensors,
  Analytics,
  Automation,
  Settings,
  Diagnostics,
];
function Workspace() {
  const collapsed = useUI((s) => s.collapsed),
    toast = useUI((s) => s.toast);
  const config = useConfig((s) => s.config),
    status = useConnection((s) => s.status),
    error = useConnection((s) => s.error);
  const live = useLive((s) => s.live);
  useEffect(() => connect(), []);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: light)");
    const apply = () => {
      document.documentElement.dataset.theme =
        config?.display.theme === "system"
          ? media.matches
            ? "light"
            : "dark"
          : config?.display.theme || "dark";
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [config?.display.theme]);
  return (
    <div className={`workspace ${collapsed ? "collapsed" : ""}`}>
      <a
        className="skip"
        href="#content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("content")?.focus();
        }}
      >
        Skip to controls
      </a>
      <aside className="sidebar">
        <div className="brand">Fan Control</div>
        <button onClick={toggleSidebar} aria-expanded={!collapsed}>
          {collapsed ? "Expand" : "Collapse"} menu
        </button>
        <nav aria-label="Workspace">
          {pages.map((page, i) => (
            <NavLink key={page} to={`/${page.toLowerCase()}`} title={page}>
              <span className="nav-index" aria-hidden="true">
                {String(i + 1).padStart(2, "0")}
              </span>
              <span className="nav-label">{page}</span>
            </NavLink>
          ))}
        </nav>
        <button onClick={() => useUI.setState({ palette: true })}>
          Commands <small>Ctrl K</small>
        </button>
        <small className="sidebar-foot">Daemon owns control</small>
      </aside>
      <div className="workarea">
        <header className="status-strip">
          <span className={status === "connected" ? "" : "warning"}>
            {status.replaceAll("-", " ")}
          </span>
          <span>
            {live?.effective_mode || "Waiting"} /{" "}
            {live?.effective_profile || "Waiting"}
          </span>
          <TempBadge value={live?.control_temp} />
          <span>{live?.backend_state || "No backend"}</span>
          {live?.demo && <b>Simulated hardware</b>}
          {!!live?.active_rules?.length && (
            <span>{live.active_rules.length} active rule(s)</span>
          )}
        </header>
        {status !== "connected" && (
          <div className="notice warning" role="status">
            {status === "stale"
              ? "Live readings are stale. Showing the last known values."
              : error || "Connecting to fan-daemon…"}
          </div>
        )}
        {live?.critical_active && (
          <div className="notice critical" role="alert">
            Critical temperature. Thermal protection overrides normal fan
            limits.
          </div>
        )}
        {!!live?.fault_missing && (
          <div className="notice warning">
            Control temperature unavailable. See Diagnostics for fallback
            status.
          </div>
        )}
        <main
          id="content"
          tabIndex={-1}
          className={status !== "connected" ? "stale" : ""}
        >
          {config ? (
            <Routes>
              {pages.map((page, i) => {
                const View = views[i];
                return (
                  <Route
                    key={page}
                    path={`/${page.toLowerCase()}`}
                    element={<View />}
                  />
                );
              })}
              <Route path="*" element={<Navigate to="/overview" replace />} />
            </Routes>
          ) : (
            <Skeleton label="Waiting for a compatible daemon configuration…" />
          )}
        </main>
      </div>
      <CommandPalette />
      {toast && (
        <div className="toast" role="status">
          {toast}
          <button
            onClick={() => useUI.setState({ toast: null })}
            aria-label="Dismiss message"
          >
            Close
          </button>
        </div>
      )}
      {live?.backend_error && (
        <div className="notice critical" role="alert">
          Backend error: {live.backend_error}. See Diagnostics.
        </div>
      )}
    </div>
  );
}
export default function App() {
  return (
    <HashRouter>
      <Workspace />
    </HashRouter>
  );
}
