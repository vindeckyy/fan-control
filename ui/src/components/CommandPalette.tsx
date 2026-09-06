import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Dialog } from "./ui";
import { act, useConfig } from "../stores/configStore";
import { useUI } from "../stores/uiStore";
import { useDisabled } from "../pages/shared";
const routes = [
  "Overview",
  "Fans",
  "Curves",
  "Sensors",
  "Analytics",
  "Automation",
  "Settings",
  "Diagnostics",
];
export default function CommandPalette() {
  const open = useUI((s) => s.palette),
    config = useConfig((s) => s.config),
    [query, setQuery] = useState(""),
    navigate = useNavigate(),
    disabled = useDisabled();
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setQuery("");
        useUI.setState({ palette: !useUI.getState().palette });
      }
      if (
        !disabled &&
        config?.mode === "manual" &&
        ["[", "]"].includes(e.key) &&
        !(e.target as HTMLElement).closest(
          "input,select,textarea,dialog,[contenteditable]",
        )
      ) {
        e.preventDefault();
        const target = Math.max(
          0,
          Math.min(
            100,
            (config.fans.fan1.control.target || 0) + (e.key === "]" ? 5 : -5),
          ),
        );
        act("set", { fan: 1, pct: target });
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [config, disabled]);
  if (!open) return null;
  const commands = [
    ...routes.map((page) => ({
      label: `Open ${page}`,
      run: () => navigate(`/${page.toLowerCase()}`),
    })),
    ...(!disabled && config
      ? [
          ...["silent", "balanced", "performance"].map((profile) => ({
            label: `Set profile: ${profile}`,
            run: () => act("profile", { profile }),
          })),
          ...["curve", "manual"].map((mode) => ({
            label: `Set mode: ${mode === "curve" ? "automatic" : mode}`,
            run: () => act("mode", { mode }),
          })),
          ...Object.entries(config.curves).map(([id, curve]) => ({
            label: `Edit curve: ${curve.name}`,
            run: () => {
              useUI.setState({ selectedCurve: id });
              navigate("/curves");
            },
          })),
          ...config.rules.map((rule) => ({
            label: `${rule.enabled ? "Disable" : "Enable"} rule: ${rule.name}`,
            run: () =>
              act("rules.set", { rule: { ...rule, enabled: !rule.enabled } }),
          })),
        ]
      : []),
  ];
  return (
    <Dialog title="Commands" onClose={() => useUI.setState({ palette: false })}>
      <label className="field">
        <span>Find a page or control</span>
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="list">
        {commands
          .filter((c) => c.label.toLowerCase().includes(query.toLowerCase()))
          .map((c) => (
            <button
              key={c.label}
              onClick={() => {
                c.run();
                useUI.setState({ palette: false });
              }}
            >
              {c.label}
            </button>
          ))}
      </div>
    </Dialog>
  );
}
