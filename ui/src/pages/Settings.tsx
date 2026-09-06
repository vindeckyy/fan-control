import { useState } from "react";
import { type Config, request } from "../client";
import { ConfirmDialog, Panel, Select, Toggle } from "../components/ui";
import { act, mutate, useConfig } from "../stores/configStore";
import { useConnection } from "../stores/connectionStore";
import { toggleSidebar, useUI } from "../stores/uiStore";
import { NumberField, ResourceState, useDisabled, useResource } from "./shared";
function Safety({ config }: { config: Config }) {
  const [draft, setDraft] = useState(config.safety),
    disabled = useDisabled();
  return (
    <Panel title="Safety">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void mutate("config", { safety: draft }).catch(() => {});
        }}
      >
        <div className="form-grid">
          <NumberField
            label="Critical temperature (°C)"
            min={70}
            max={110}
            value={draft.critical_temp}
            onChange={(critical_temp) => setDraft({ ...draft, critical_temp })}
          />
          <NumberField
            label="Normal noise cap (%)"
            min={20}
            max={100}
            value={draft.global_max_duty}
            onChange={(global_max_duty) =>
              setDraft({ ...draft, global_max_duty })
            }
          />
          <NumberField
            label="Hysteresis (%)"
            min={0}
            max={30}
            value={draft.hysteresis}
            onChange={(hysteresis) => setDraft({ ...draft, hysteresis })}
          />
        </div>
        <Toggle
          label="Return to firmware after repeated missing temperatures"
          checked={draft.firmware_fallback}
          onChange={(firmware_fallback) =>
            setDraft({ ...draft, firmware_fallback })
          }
        />
        <p className="muted">
          Critical-temperature protection forces full cooling and bypasses the
          normal noise cap and per-fan limits. Policy temperatures are always
          stored in Celsius.
        </p>
        <div className="actions">
          <button
            className="primary"
            disabled={
              disabled ||
              JSON.stringify(config.safety) === JSON.stringify(draft)
            }
          >
            Save safety settings
          </button>
          <ConfirmDialog
            label="Return to EC Auto"
            title="Return control to firmware?"
            disabled={disabled}
            onConfirm={() => act("mode", { mode: "released" })}
          >
            The embedded controller will manage cooling using its firmware
            policy.
          </ConfirmDialog>
        </div>
      </form>
    </Panel>
  );
}
export default function Settings() {
  const config = useConfig((s) => s.config)!,
    caps = useConnection((s) => s.capabilities),
    collapsed = useUI((s) => s.collapsed),
    disabled = useDisabled();
  const info = useResource(() =>
    request<{
      daemon: { config_path: string; uptime_s: number };
      history: { degraded: boolean; path: string; error?: string };
    }>("diagnostics.snapshot"),
  );
  return (
    <>
      <h1>Settings</h1>
      <Safety key={config.revision} config={config} />
      <div className="two-col">
        <Panel title="Appearance">
          <Select
            label="Theme"
            value={config.display.theme}
            disabled={disabled}
            onChange={(theme) => act("config", { display: { theme } })}
          >
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="system">System</option>
          </Select>
          <Select
            label="Display temperatures"
            value={config.display.temperature_unit}
            disabled={disabled}
            onChange={(temperature_unit) =>
              act("config", { display: { temperature_unit } })
            }
          >
            <option value="c">Celsius (°C)</option>
            <option value="f">Fahrenheit (°F)</option>
          </Select>
          <Toggle
            label="Collapse sidebar"
            checked={collapsed}
            onChange={toggleSidebar}
          />
        </Panel>
        <Panel title="Notifications">
          <Toggle
            label="Desktop notifications"
            checked={config.notifications.enabled}
            disabled={disabled}
            onChange={(enabled) =>
              act("config", { notifications: { enabled } })
            }
          />
          <Toggle
            label="Critical-temperature alerts"
            checked={config.notifications.critical_temp}
            disabled={disabled}
            onChange={(critical_temp) =>
              act("config", { notifications: { critical_temp } })
            }
          />
          <p className="muted">
            Desktop alerts are delivered while the application is open.
          </p>
        </Panel>
      </div>
      <Panel title="Telemetry retention">
        <Toggle
          label="Persist telemetry to disk"
          checked={config.history.persist}
          disabled={disabled}
          onChange={(persist) => act("config", { history: { persist } })}
        />
        <Select
          label="Keep history"
          value={String(config.history.retention_days)}
          disabled={disabled}
          onChange={(retention_days) =>
            act("config", { history: { retention_days: +retention_days } })
          }
        >
          {[1, 7, 14, 30, 90].map((n) => (
            <option key={n} value={n}>
              {n} days
            </option>
          ))}
        </Select>
      </Panel>
      <Panel title="Daemon and About">
        <p>
          Fan Control {caps?.version} · Protocol {caps?.protocol_version} ·
          Configuration schema {caps?.schema_version}
        </p>
        <p>
          Backend: {caps?.backend} · Configuration revision {config.revision}
        </p>
        <ResourceState resource={info}>
          <p>Configuration: {info.data?.daemon.config_path}</p>
          <p>
            History: {info.data?.history.degraded ? "Degraded" : "Available"} ·{" "}
            {info.data?.history.path}
          </p>
          <p>
            Daemon uptime: {Math.round(info.data?.daemon.uptime_s || 0)} seconds
          </p>
        </ResourceState>
      </Panel>
    </>
  );
}
