import { useState } from "react";
import { client, request, type Rule, type Schedule } from "../client";
import {
  ConfirmDialog,
  Dialog,
  EmptyState,
  Panel,
  Select,
  Toggle,
} from "../components/ui";
import { act, mutate, useConfig } from "../stores/configStore";
import {
  Feature,
  NumberField,
  ResourceState,
  useDisabled,
  useResource,
} from "./shared";
export const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export function newRule(): Rule {
  return {
    id: `rule_${Date.now()}`,
    name: "",
    enabled: true,
    priority: 100,
    trigger: { type: "temp_above", sensor: "gpu", value: 75 },
    condition: { sustain_ticks: 3, cooldown_seconds: 30 },
    action: { type: "set_profile", profile: "performance" },
  };
}
export function triggerFor(type: string): Rule["trigger"] {
  if (type === "time_range")
    return { type, days: [...days], start: "23:00", end: "07:00" };
  if (type === "on_startup") return { type };
  if (type === "rpm_below") return { type, fan: "fan1", value: 500 };
  return { type, sensor: "gpu", value: 75 };
}
export function actionFor(type: string): Rule["action"] {
  if (type === "set_mode") return { type, mode: "auto" };
  if (type === "set_duty") return { type, fan: "fan1", pct: 60 };
  if (type === "notify") return { type, message: "" };
  return { type, profile: "performance" };
}
function DayPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <fieldset className="day-picker">
      <legend>Days</legend>
      {days.map((d) => (
        <Toggle
          key={d}
          label={d}
          checked={value.includes(d)}
          onChange={(checked) =>
            onChange(checked ? [...value, d] : value.filter((v) => v !== d))
          }
        />
      ))}
    </fieldset>
  );
}
function RuleEditor({ rule, close }: { rule: Rule; close: () => void }) {
  const [draft, setDraft] = useState(rule),
    [explanation, setExplanation] = useState(""),
    [testing, setTesting] = useState(false),
    disabled = useDisabled();
  const temp = draft.trigger.type.startsWith("temp_");
  return (
    <Dialog title={rule.name ? "Edit rule" : "Create rule"} onClose={close}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void mutate("rules.set", { rule: draft })
            .then(close)
            .catch(() => {});
        }}
      >
        <label className="field">
          <span>Rule name</span>
          <input
            required
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
        </label>
        <Select
          label="Trigger"
          value={draft.trigger.type}
          onChange={(type) => setDraft({ ...draft, trigger: triggerFor(type) })}
        >
          <option value="temp_above">Temperature above</option>
          <option value="temp_below">Temperature below</option>
          <option value="rpm_below">RPM below</option>
          <option value="time_range">Time range</option>
          <option value="on_startup">Daemon startup</option>
        </Select>
        {temp && (
          <Select
            label="Sensor alias"
            value={draft.trigger.sensor || "gpu"}
            onChange={(sensor) =>
              setDraft({ ...draft, trigger: { ...draft.trigger, sensor } })
            }
          >
            <option value="cpu">CPU</option>
            <option value="gpu">GPU</option>
            <option value="max">Hottest</option>
          </Select>
        )}
        {draft.trigger.type === "rpm_below" && (
          <Select
            label="Fan"
            value={draft.trigger.fan || "fan1"}
            onChange={(fan) =>
              setDraft({ ...draft, trigger: { ...draft.trigger, fan } })
            }
          >
            {["fan1", "fan2", "fan3"].map((f) => (
              <option key={f}>{f}</option>
            ))}
          </Select>
        )}
        {(temp || draft.trigger.type === "rpm_below") && (
          <NumberField
            label={temp ? "Threshold (°C)" : "Threshold (RPM)"}
            value={draft.trigger.value || 0}
            min={0}
            max={temp ? 150 : 20000}
            onChange={(value) =>
              setDraft({ ...draft, trigger: { ...draft.trigger, value } })
            }
          />
        )}
        {draft.trigger.type === "time_range" && (
          <>
            <DayPicker
              value={draft.trigger.days || []}
              onChange={(days) =>
                setDraft({ ...draft, trigger: { ...draft.trigger, days } })
              }
            />
            <div className="form-grid">
              {(["start", "end"] as const).map((key) => (
                <label className="field" key={key}>
                  <span>{key}</span>
                  <input
                    required
                    type="time"
                    value={draft.trigger[key]}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        trigger: { ...draft.trigger, [key]: e.target.value },
                      })
                    }
                  />
                </label>
              ))}
            </div>
          </>
        )}
        <div className="form-grid">
          <NumberField
            label="Sustain (ticks)"
            value={draft.condition.sustain_ticks}
            min={0}
            max={1000}
            onChange={(sustain_ticks) =>
              setDraft({
                ...draft,
                condition: { ...draft.condition, sustain_ticks },
              })
            }
          />
          <NumberField
            label="Cooldown (seconds)"
            value={draft.condition.cooldown_seconds}
            min={0}
            max={86400}
            onChange={(cooldown_seconds) =>
              setDraft({
                ...draft,
                condition: { ...draft.condition, cooldown_seconds },
              })
            }
          />
          <NumberField
            label="Priority"
            value={draft.priority}
            min={0}
            max={1000}
            onChange={(priority) => setDraft({ ...draft, priority })}
          />
        </div>
        <Select
          label="Action"
          value={draft.action.type}
          onChange={(type) => setDraft({ ...draft, action: actionFor(type) })}
        >
          <option value="set_profile">Set profile</option>
          <option value="set_mode">Set mode</option>
          <option value="set_duty">Set duty</option>
          <option value="notify">Desktop notification</option>
        </Select>
        {draft.action.type === "set_profile" && (
          <Select
            label="Profile"
            value={draft.action.profile || "performance"}
            onChange={(profile) =>
              setDraft({ ...draft, action: { ...draft.action, profile } })
            }
          >
            {["silent", "balanced", "performance", "custom"].map((p) => (
              <option key={p}>{p}</option>
            ))}
          </Select>
        )}
        {draft.action.type === "set_mode" && (
          <Select
            label="Mode"
            value={draft.action.mode || "auto"}
            onChange={(mode) =>
              setDraft({ ...draft, action: { ...draft.action, mode } })
            }
          >
            <option value="auto">Automatic</option>
            <option value="manual">Manual</option>
            <option value="released">EC automatic</option>
          </Select>
        )}
        {draft.action.type === "set_duty" && (
          <>
            <Select
              label="Target fan"
              value={draft.action.fan || "fan1"}
              onChange={(fan) =>
                setDraft({ ...draft, action: { ...draft.action, fan } })
              }
            >
              {["fan1", "fan2", "fan3"].map((f) => (
                <option key={f}>{f}</option>
              ))}
            </Select>
            <NumberField
              label="Requested duty (%)"
              value={draft.action.pct || 0}
              min={0}
              max={100}
              onChange={(pct) =>
                setDraft({ ...draft, action: { ...draft.action, pct } })
              }
            />
          </>
        )}
        {draft.action.type === "notify" && (
          <label className="field">
            <span>Notification message</span>
            <input
              required
              value={draft.action.message || ""}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  action: { ...draft.action, message: e.target.value },
                })
              }
            />
          </label>
        )}
        <div className="actions">
          <button
            type="button"
            disabled={disabled || testing}
            onClick={async () => {
              setTesting(true);
              try {
                const reply = await request<{
                  reason: string;
                  matched: boolean;
                  sustain_ticks: number;
                }>("rules.test", { rule: draft });
                setExplanation(
                  `${reply.matched ? "Matches" : "Does not match"}: ${reply.reason}. Requires ${reply.sustain_ticks} sustained ticks. No action applied.`,
                );
              } catch (e) {
                setExplanation((e as Error).message);
              } finally {
                setTesting(false);
              }
            }}
          >
            Test rule
          </button>
          <button className="primary" disabled={disabled}>
            Save rule
          </button>
        </div>
        {explanation && <p role="status">{explanation}</p>}
      </form>
    </Dialog>
  );
}
function ScheduleEditor({ saved }: { saved: Schedule }) {
  const [draft, setDraft] = useState(saved),
    disabled = useDisabled();
  const change = (i: number, patch: Partial<Schedule["items"][number]>) =>
    setDraft({
      ...draft,
      items: draft.items.map((item, j) =>
        j === i ? { ...item, ...patch } : item,
      ),
    });
  return (
    <Panel title="Weekly schedule">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void mutate("schedule.set", { schedule: draft }).catch(() => {});
        }}
      >
        <Toggle
          label="Enable schedule"
          checked={draft.enabled}
          onChange={(enabled) => setDraft({ ...draft, enabled })}
        />
        <label className="field">
          <span>Timezone (local or IANA zone)</span>
          <input
            required
            value={draft.timezone}
            onChange={(e) => setDraft({ ...draft, timezone: e.target.value })}
          />
        </label>
        <div className="week-grid">
          {days.map((day) => (
            <div key={day}>
              <b>{day}</b>
              {draft.items
                .filter((item) => item.days.includes(day))
                .map((item) => (
                  <p key={item.id}>
                    {item.start}–{item.end}
                    <br />
                    {[item.profile, item.mode].filter(Boolean).join(" · ") || "No change"}
                  </p>
                ))}
            </div>
          ))}
        </div>
        <p className="muted">
          An end time earlier than its start continues into the next day. When
          blocks overlap, the last matching block wins.
        </p>
        {draft.items.map((item, i) => (
          <fieldset key={item.id}>
            <legend>Block {i + 1}</legend>
            <DayPicker
              value={item.days}
              onChange={(days) => change(i, { days })}
            />
            <div className="form-grid">
              {(["start", "end"] as const).map((key) => (
                <label key={key} className="field">
                  <span>{key}</span>
                  <input
                    required
                    type="time"
                    value={item[key]}
                    onChange={(e) => change(i, { [key]: e.target.value })}
                  />
                </label>
              ))}
              <Select
                label="Profile"
                value={item.profile || ""}
                onChange={(profile) => change(i, { profile: profile || undefined })}
              >
                <option value="">Keep current</option>
                {["silent", "balanced", "performance", "custom"].map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </Select>
              <Select
                label="Mode"
                value={item.mode || ""}
                onChange={(mode) => change(i, { mode: mode || undefined })}
              >
                <option value="">Keep current</option>
                {["auto", "manual", "released"].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </Select>
            </div>
            <button
              type="button"
              onClick={() =>
                setDraft({
                  ...draft,
                  items: draft.items.filter((_, j) => j !== i),
                })
              }
            >
              Remove block
            </button>
          </fieldset>
        ))}
        <div className="actions">
          <button
            type="button"
            onClick={() =>
              setDraft({
                ...draft,
                items: [
                  ...draft.items,
                  {
                    id: `block_${Date.now()}`,
                    days: [...days],
                    start: "23:00",
                    end: "07:00",
                    profile: "silent",
                  },
                ],
              })
            }
          >
            Add schedule block
          </button>
          <button
            className="primary"
            disabled={
              disabled || JSON.stringify(saved) === JSON.stringify(draft)
            }
          >
            Save schedule
          </button>
        </div>
      </form>
    </Panel>
  );
}
function AutomationBody() {
  const config = useConfig((s) => s.config)!,
    resource = useResource(client.rules, [config.revision], 2000),
    [editing, setEditing] = useState<Rule | null>(null),
    disabled = useDisabled();
  return (
    <>
      <Panel
        title="Rules"
        actions={
          <button
            className="primary"
            disabled={disabled}
            onClick={() => setEditing(newRule())}
          >
            Create rule
          </button>
        }
      >
        <p className="muted">
          Rules temporarily override the saved policy. Higher priority wins;
          configuration order breaks ties. Command execution is unavailable in
          this release.
        </p>
        <ResourceState resource={resource}>
          {!config.rules.length ? (
            <EmptyState>
              No automation rules. Create one to react to temperatures, fan
              speed, or time.
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Trigger / action</th>
                    <th>Priority</th>
                    <th>State</th>
                    <th>Manage</th>
                  </tr>
                </thead>
                <tbody>
                  {(resource.data?.rules || []).map((rule) => (
                    <tr key={rule.id}>
                      <td>
                        {rule.name}
                        <Toggle
                          label="Enabled"
                          checked={rule.enabled}
                          disabled={disabled}
                          onChange={(enabled) =>
                            act("rules.set", { rule: { ...rule, enabled } })
                          }
                        />
                      </td>
                      <td>
                        {rule.trigger.type.replaceAll("_", " ")}
                        <br />
                        {rule.action.type.replaceAll("_", " ")}{" "}
                        {rule.action.profile || rule.action.mode || ""}
                      </td>
                      <td>{rule.priority}</td>
                      <td>{rule.state}</td>
                      <td>
                        <div className="actions">
                          <button
                            disabled={disabled}
                            onClick={() => setEditing(rule)}
                          >
                            Edit rule
                          </button>
                          <ConfirmDialog
                            label="Delete rule"
                            title="Delete this rule?"
                            disabled={disabled}
                            onConfirm={() =>
                              act("rules.delete", { id: rule.id })
                            }
                          >
                            Its overlay will stop on the next control tick.
                          </ConfirmDialog>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </ResourceState>
      </Panel>
      <Feature feature="schedule" name="scheduling">
        <ScheduleEditor key={config.revision} saved={config.schedule} />
      </Feature>
      {editing && <RuleEditor rule={editing} close={() => setEditing(null)} />}
    </>
  );
}
export default function Automation() {
  return (
    <>
      <h1>Automation</h1>
      <Feature feature="rules" name="Automation">
        <AutomationBody />
      </Feature>
    </>
  );
}
