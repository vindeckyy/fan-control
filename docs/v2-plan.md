# fan-control v2: Full Product, UI, Daemon, and Architecture Redesign

## 0. Objective

Rebuild `fan-control` into a cohesive desktop fan-management application rather than a single-page hardware dashboard.

The existing React 18 + Vite + WebKitGTK architecture remains, but the frontend is replaced by a multi-page workspace application with dedicated surfaces for:

`Overview`, `Fans`, `Curves`, `Sensors`, `Analytics`, `Automation`, `Settings`, and `Diagnostics`.

The daemon remains authoritative for all hardware control and receives targeted architectural upgrades required to support the new UI correctly:

- versioned configuration with safe migration
- independent per-fan control policies
- reusable named curves
- stable sensor identities
- deterministic automation/rules
- scheduling
- persistent telemetry
- decision diagnostics
- capability discovery
- expanded typed RPC
- better failure handling
- clearer separation between persistent configuration and transient runtime state

The old dashboard is retired rather than incrementally restyled.

The redesign must preserve all currently supported hardware behavior and existing features unless explicitly documented otherwise.

The application must remain usable and releasable at the end of every implementation phase.

---

# 1. Architectural invariants

Several rules should be established before implementation because they affect almost every subsequent decision.

### The daemon is always authoritative

The frontend never decides the final fan duty written to hardware.

The UI may preview calculations, display expected values, edit curves, test rules, and request temporary overrides, but the actual control decision always passes through the daemon's complete policy and safety pipeline.

This prevents frontend bugs, stale state, disconnected windows, or WebKit crashes from creating unsafe hardware behavior.

### Safety always outranks user policy

The effective control precedence is:

```text
hardware safety / critical-temperature protection
                ↓
temporary diagnostic or fan-test override
                ↓
automation rule overlay
                ↓
schedule overlay
                ↓
persistent user-selected mode/profile
                ↓
curve / manual target resolution
                ↓
fan-specific min/max constraints
                ↓
hysteresis / slew / write suppression
                ↓
backend write
```

Critical-temperature behavior must be capable of bypassing normal noise caps and user-configured maximum duty when necessary.

A user setting such as "maximum 60%" must never prevent the safety subsystem from commanding 100% during a thermal emergency.

### Automation does not silently rewrite persistent configuration

Rules and schedules produce temporary runtime policy overlays.

For example, a rule with `set_profile: performance` should make the effective profile `performance` while the rule applies. It should not modify the user's saved default profile every control tick.

The distinction must be exposed in runtime state:

```text
configured_profile
effective_profile
effective_profile_source
```

The same principle applies to mode and duty overrides.

### Control behavior must be deterministic

Given the same:

```text
configuration
sensor snapshot
rule state
schedule state
runtime override state
```

the policy engine must produce the same decision.

Policy computation should therefore be isolated from hardware I/O wherever practical and heavily unit-tested as pure logic.

### UI mutations are acknowledged, not assumed

Hardware-control changes should not rely on optimistic UI state.

The UI requests a change, receives the daemon's accepted state and new configuration revision, and then updates its store.

This prevents the interface from displaying a configuration the daemon rejected or normalized differently.

---

# 2. Target daemon control pipeline

Refactor the control loop into explicit stages.

Each tick should conceptually execute:

```text
1. Read backend sensors
2. Normalize sensor snapshot
3. Detect invalid/stale/missing values
4. Evaluate schedules
5. Evaluate rules
6. Resolve effective runtime policy
7. Resolve each fan's control source
8. Evaluate curves/manual targets
9. Apply per-fan bounds
10. Apply thermal safety override
11. Apply hysteresis / write suppression / optional slew limits
12. Write required hardware changes
13. Record decision trace
14. Append telemetry sample
15. Publish live state
```

Do not evaluate automation only after the fan decision has already been made. Doing so introduces an unnecessary one-tick delay and complicates reasoning about which rule caused which hardware action.

The daemon should produce an internal `ControlDecision` object every tick containing enough information to explain exactly what happened.

Example:

```python
ControlDecision(
    timestamp=...,
    fan_id="fan1",
    sensor_id="hwmon:...",
    source_temp=72.0,
    configured_curve="cpu_default",
    requested_duty=61,
    bounded_duty=61,
    final_duty=61,
    previous_duty=58,
    effective_mode="custom",
    effective_profile="balanced",
    policy_source="rule:gaming",
    safety_override=False,
    write_performed=True,
    write_suppressed_reason=None,
)
```

Keep a bounded in-memory ring buffer of these decisions for Diagnostics.

---

# Phase 1: Daemon foundation

## 3. Configuration v2

### 3.1 Introduce an explicit versioned schema

Replace the loosely evolving configuration structure with a schema owned by `fan_policy.py`.

A representative v2 document:

```json
{
  "version": 2,
  "revision": 1,

  "mode": "auto",
  "profile": "balanced",

  "safety": {
    "critical_temp": 95,
    "global_max_duty": 100,
    "hysteresis": 2,
    "firmware_fallback": true
  },

  "display": {
    "theme": "dark",
    "temperature_unit": "c"
  },

  "notifications": {
    "enabled": true,
    "critical_temp": true
  },

  "fans": {
    "fan1": {
      "name": "CPU Fan",
      "enabled": true,
      "control": {
        "type": "curve",
        "curve_ref": "cpu_default"
      },
      "min_duty": 0,
      "max_duty": 100
    }
  },

  "curves": {
    "cpu_default": {
      "name": "CPU Default",
      "temp_source": "cpu",
      "points": [
        [35, 20],
        [50, 35],
        [65, 55],
        [80, 80],
        [90, 100]
      ]
    }
  },

  "rules": [],

  "schedule": {
    "enabled": false,
    "timezone": "local",
    "items": []
  },

  "history": {
    "persist": true,
    "retention_days": 7
  }
}
```

Avoid encoding overloaded semantics through combinations such as:

```text
curve_ref = null
linked = true
```

where possible.

Prefer explicit states.

For example:

```json
"control": {
  "type": "linked"
}
```

or:

```json
"control": {
  "type": "curve",
  "curve_ref": "gpu_quiet"
}
```

This removes a large class of ambiguous combinations.

### 3.2 Configuration revisions

Every successful persistent mutation increments:

```text
revision
```

RPC responses expose:

```text
config_revision
```

Mutating RPC methods may optionally accept:

```text
expected_revision
```

If the UI attempts to mutate stale configuration, return a revision-conflict error rather than silently overwriting a newer state.

This becomes especially useful once multiple interfaces exist:

```text
GUI
tray
fan-ctl
future integrations
```

### 3.3 Migration

Add:

```python
default_config_v2()
detect_config_version()
upgrade_config_v1_to_v2()
validate_config()
normalize_config()
migrate_config()
```

The migration must map existing:

```text
curve
curve_cpu
curve_gpu
named_curves
targets
linked
theme
alerts
profile
mode
```

into the v2 representation without losing behavior.

Curve IDs generated from v1 configuration should be deterministic so the same configuration does not generate different identifiers between migrations.

For example:

```text
cpu_default
gpu_default
legacy_custom
named_<sanitized-name>
```

Resolve collisions deterministically.

### 3.4 Migration safety

Before the first v1 to v2 write:

```text
config.json
        ↓
config.v1.backup.json
```

Then write the migrated configuration through:

```text
temporary file
fsync
atomic rename
```

Never truncate the original file before a valid replacement exists.

Unknown configuration keys should be retained whenever possible rather than discarded.

If validation of the migrated document fails, keep the original configuration untouched and start using a safe compatibility path.

### 3.5 Centralized validation

Do not scatter range validation throughout individual RPC handlers.

Configuration validation should centrally enforce constraints such as:

```text
0 <= duty <= 100
0 <= min_duty <= max_duty <= 100
curve temperatures monotonically increase
curve duty values remain valid
curve has sufficient points
rule IDs unique
curve IDs unique
fan references valid
retention_days bounded
critical temperature sane
schedule times valid
```

Normalization may clamp or canonicalize explicitly documented values, but invalid structural input should produce a clear error.

---

# 4. Per-fan policy model

Replace legacy shared `curve_cpu` / `curve_gpu` decision branching with independent fan policy resolution.

Each fan independently resolves:

```text
enabled
control type
curve
temperature source
requested duty
minimum duty
maximum duty
```

Supported control types should initially be:

```text
linked
curve
manual
```

`linked` means the daemon determines the hottest eligible control source.

`curve` means the fan follows the named curve and that curve's configured temperature source.

`manual` means the fan follows an explicit target.

If the old UI or CLI submits the legacy:

```text
custom {curve, which}
```

RPC, implement it as a compatibility translation layer over the new configuration.

Do not maintain two separate internal control systems.

### Missing references

A deleted or unavailable curve/sensor must have deterministic behavior.

For example:

```text
missing curve_ref
    ↓
record configuration warning
    ↓
use safe default curve
    ↓
expose fallback in diagnostics
```

Never silently reinterpret an invalid reference as zero duty.

---

# 5. Stable sensor identity

The proposed identity:

```text
name:label:hwmon-index
```

should not be used as the canonical persistent identifier.

Linux `hwmonX` indices are not guaranteed to remain stable across boot or driver enumeration changes.

Instead derive persistent sensor IDs from stable hardware identity wherever available.

For hwmon:

```text
chip / driver identity
device syspath or physical device identity
channel
label
```

Conceptually:

```text
hwmon:<device-identity>:temp2
```

For NVIDIA devices, prefer a stable GPU UUID or PCI identity.

The current `hwmon-index` can still be included as runtime metadata for diagnostics.

`sensors.list` should expose records resembling:

```json
{
  "id": "hwmon:nct6798:platform-nct6775.656:temp2",
  "name": "nct6798",
  "label": "CPU",
  "channel": "temp2",
  "kind": "temperature",
  "value": 56.0,
  "unit": "c",
  "source": "hwmon",
  "runtime_path": "/sys/class/hwmon/hwmon4/temp2_input",
  "available": true
}
```

Also expose aliases:

```text
cpu
gpu
max
```

These are policy selectors, not persistent hardware IDs.

If a previously configured sensor disappears, report it as unavailable and expose the fallback source being used.

---

# 6. Rules engine

Create:

```text
fan_rules.py
```

The core evaluator should be independent from backend I/O.

A rule should include explicit precedence and timing behavior:

```json
{
  "id": "gaming",
  "name": "Gaming cooling",
  "enabled": true,
  "priority": 100,

  "trigger": {
    "type": "temp_above",
    "sensor": "gpu",
    "value": 70
  },

  "condition": {
    "sustain_ticks": 3,
    "cooldown_seconds": 30
  },

  "action": {
    "type": "set_profile",
    "profile": "performance"
  }
}
```

Supported trigger classes:

```text
temp_above
temp_below
rpm_below
time_range
on_startup
```

Supported actions:

```text
set_profile
set_mode
set_duty
notify
run_command
```

### 6.1 Deterministic conflict resolution

Do not use vague "first match per trigger type" semantics.

Different trigger types may target the same output.

Instead every resulting action participates in deterministic arbitration based on:

```text
safety class
rule priority
configuration order as final tie-breaker
```

The evaluator should distinguish:

```text
matched
sustaining
active
cooldown
applied
shadowed
```

A rule can therefore be active but shadowed by a higher-priority rule.

Expose this to Diagnostics and Automation.

### 6.2 Stateful rule runtime

Persistent configuration and rule runtime state must remain separate.

Runtime state may contain:

```text
consecutive_match_count
active_since
last_fired_at
cooldown_until
startup_consumed
```

This should not cause configuration-file writes every tick.

### 6.3 Rule testing

`rules.test` evaluates against a supplied sensor snapshot without applying any action.

Return a detailed explanation:

```json
{
  "matched": true,
  "would_activate": true,
  "action": {...},
  "reason": "gpu 76.0 C > threshold 70.0 C",
  "priority": 100
}
```

This makes the Automation editor genuinely useful rather than merely returning true/false.

### 6.4 `run_command` security

This is a meaningful privilege boundary because the daemon may run with elevated permissions.

It must therefore be disabled by default.

If implemented, require:

```text
explicit administrator allowlist
absolute executable paths
argv arrays only
no shell=True
no arbitrary command strings
sanitized environment
execution timeout
output-size limit
concurrency limit
audit logging
```

An empty allowlist means no command execution is possible.

A rule must reference an allowlisted command ID rather than supply arbitrary executable text.

Example:

```json
{
  "type": "run_command",
  "command_ref": "performance-mode"
}
```

---

# 7. Scheduling

Scheduling should use the same transient-policy concept as rules.

Example:

```json
{
  "enabled": true,
  "timezone": "local",
  "items": [
    {
      "id": "night",
      "days": ["mon", "tue", "wed", "thu", "fri"],
      "start": "23:00",
      "end": "07:00",
      "profile": "quiet"
    }
  ]
}
```

Clearly define overnight ranges such as:

```text
23:00 → 07:00
```

as crossing midnight.

Use Python `zoneinfo` semantics rather than hand-written UTC offset calculations.

Schedules should be tested around:

```text
midnight
week boundaries
DST transitions
disabled schedules
overlapping schedule blocks
```

Overlapping schedules need deterministic precedence.

Prefer explicit schedule priority or well-defined last-entry precedence.

---

# 8. Persistent history

Create:

```text
fan_history.py
```

Database location:

```text
/var/lib/fan-control/history.db
```

Directory:

```text
0750 root:fan-control
```

Use SQLite through Python's standard library.

### 8.1 Prefer normalized storage over large JSON metric blobs

A JSON-only history row makes later analytics, per-fan statistics, sensor queries, and aggregation unnecessarily expensive.

Use a small normalized schema.

For example:

```sql
samples
-------
id
ts
mode
profile
effective_mode
effective_profile
hottest_temp

fan_samples
-----------
sample_id
fan_id
rpm
duty
requested_duty

sensor_samples
--------------
sample_id
sensor_id
value
```

Indexes should cover at least:

```text
samples(ts)
fan_samples(sample_id, fan_id)
sensor_samples(sample_id, sensor_id)
```

If storing every discovered sensor every 500 ms proves excessive, define a telemetry allowlist containing the primary control sensors plus user-pinned sensors.

### 8.2 Database operating mode

Initialize with appropriate SQLite settings such as WAL mode where supported.

Use a bounded write batch, for example every 10 control ticks.

A database error must never halt fan control.

Failure behavior:

```text
database write fails
        ↓
mark history persistence degraded
        ↓
continue in-memory telemetry
        ↓
surface diagnostics warning
        ↓
retry later
```

### 8.3 Retention

Prune according to:

```text
history.retention_days
```

Run pruning:

```text
at startup
and periodically thereafter
```

not only at startup, because long-running systems may remain online for weeks.

Avoid aggressive `VACUUM` operations during normal control operation.

### 8.4 Query contract

Prefer:

```text
history.query {
    since,
    until,
    sensors?,
    fans?,
    max_points?
}
```

rather than making the frontend choose implementation-specific downsampling algorithms.

The daemon can select:

```text
raw
bucket aggregation
LTTB
```

based on requested range and `max_points`.

The UI asks for presentation requirements, not storage implementation details.

---

# 9. RPC v2

Keep the existing transport to minimize scope, but formalize the application protocol.

Add a daemon capability endpoint:

```text
capabilities
```

returning:

```json
{
  "protocol_version": 2,
  "schema_version": 2,
  "features": [
    "rules",
    "history",
    "per_fan_curves",
    "schedule",
    "decision_trace"
  ],
  "backend": "...",
  "fan_count": 3
}
```

This allows a newer UI to degrade gracefully against an older daemon.

### 9.1 New methods

Implement:

```text
capabilities

fans.list
fans.rename
fans.configure
fans.test

curves.list
curves.set
curves.delete
curves.assign

sensors.list

rules.list
rules.set
rules.delete
rules.test

schedule.get
schedule.set

history.query
history.stats

diagnostics.snapshot
diagnostics.decisions

snapshot
live
```

Retain legacy RPC methods as adapters where reasonable.

### 9.2 Standard mutation response

Mutations should return authoritative state.

Example:

```json
{
  "ok": true,
  "config_revision": 37,
  "changed": {...}
}
```

Do not require the UI to guess what normalization occurred.

### 9.3 Standard errors

Introduce stable machine-readable errors:

```text
INVALID_ARGUMENT
NOT_FOUND
REVISION_CONFLICT
UNSUPPORTED
BACKEND_ERROR
PERMISSION_DENIED
CONFIG_ERROR
HISTORY_UNAVAILABLE
```

Responses should include both:

```text
code
human-readable message
```

The frontend should branch on the code, not parse strings.

### 9.4 Live-state sequencing

Every live payload should contain:

```text
seq
timestamp
```

The UI can then detect:

```text
stale state
dropped updates
daemon restart
```

Live data should include:

```text
temperatures
fan rpm
fan duty
effective mode
effective profile
configured mode
configured profile
active rules
policy source
backend state
critical state
config revision
```

Keep large configuration data out of the 500 ms live stream.

---

# 10. Testing foundation

Before the frontend rewrite, add a deterministic fake backend.

Example:

```text
FakeFanBackend
```

It should support scripted:

```text
temperatures
RPM
fan duty writes
sensor appearance/disappearance
write failures
read failures
```

This enables complete control-loop tests without physical hardware.

Phase 1 test coverage must include:

```text
v1 to v2 migration
migration failure recovery
atomic config persistence
configuration validation
per-fan curve resolution
linked source selection
missing curve fallback
missing sensor fallback
manual control
fan bounds
critical-temperature override
hysteresis
rule triggers
rule priority
rule shadowing
sustain
cooldown
startup rules
overnight schedules
SQLite batching
database failure fallback
retention pruning
history querying
RPC argument validation
RPC revision conflicts
legacy RPC compatibility
```

Test against supported:

```text
Python 3.10
Python 3.12
Python 3.13
```

Phase 1 is complete only when the existing application can still run against the new daemon.

---

# Phase 2: Frontend platform

## 11. Application architecture

Rewrite the frontend around four layers:

```text
routes/pages
      ↓
domain components
      ↓
shared UI primitives
      ↓
typed daemon client + stores
```

Avoid allowing individual components to call the raw bridge directly.

The bridge belongs behind a typed client API.

---

# 12. Routing

Add `react-router-dom`.

Use hash routing exclusively because the application is hosted under the custom WebKit scheme.

Routes:

```text
/#/overview
/#/fans
/#/curves
/#/sensors
/#/analytics
/#/automation
/#/settings
/#/diagnostics
```

The initial Phase 2 acceptance test must explicitly verify routing through:

```text
fancontrol://...
```

inside WebKitGTK before page development begins.

No browser-history/path routing should accidentally depend on an HTTP server.

---

# 13. State architecture

Use Zustand, but separate state based on update frequency and responsibility.

Conceptually:

```text
connectionStore
liveStore
configStore
uiStore
```

`liveStore` receives high-frequency telemetry.

`configStore` contains snapshot/configuration state.

`uiStore` contains presentation-only state such as:

```text
sidebar state
selected curve
temperature display unit
dialog visibility
```

Components must subscribe through narrow selectors.

A fan RPM component should not rerender because:

```text
a rule name changed
theme changed
history query completed
```

### Mutation flow

Use:

```text
user mutation
      ↓
RPC request with current revision
      ↓
daemon validates and commits
      ↓
RPC returns authoritative revision/state
      ↓
config store updated
```

After significant multi-object mutations, optionally perform a complete `snapshot` reconciliation.

Do not blindly fetch a full snapshot after every slider movement if that creates unnecessary IPC traffic.

---

# 14. Connection lifecycle

On connection:

```text
capabilities
snapshot
live subscription
```

Track:

```text
connected
reconnecting
stale
unsupported-version
daemon-error
```

If the most recent live message exceeds five seconds:

```text
mark live readings stale
visually de-emphasize values
stop presenting stale telemetry as current
```

Do not reset gauges to zero, since zero could be interpreted as a real measurement.

Display the last known reading with a stale treatment and age indicator.

---

# 15. Live event transport

Avoid emitting an independent JavaScript invocation for every rapidly changing property.

Batch daemon events into coherent live payloads.

Coalesce redundant updates if the UI thread is behind.

`rules_active`, critical-state transitions, and backend-state changes can remain explicit events if needed, but avoid creating parallel inconsistent sources of truth.

---

# 16. Design system

Create:

```text
ui/src/theme/
ui/src/components/ui/
```

Build a two-layer token system.

Primitive tokens:

```text
--c-bg-0
--c-bg-1
--c-bg-2
--c-bg-3
--c-accent
--c-warning
--c-danger

--space-1 ...
--radius-1 ...
--font-size-1 ...
```

Semantic tokens:

```text
--surface-app
--surface-card
--surface-raised
--border-subtle
--text-primary
--text-secondary
--reading-normal
--reading-hot
--reading-critical
```

The visual identity is dark-first and instrumentation-oriented rather than generic SaaS.

Use:

```text
near-black layered surfaces
subtle borders
limited accent usage
cyan/teal normal-state accent
amber warning state
red critical state
tabular numerals
dense but readable 13px interface typography
```

Avoid unnecessary gradients, excessive glow, huge rounded cards, floating glass surfaces, and decorative whitespace that reduces information density.

Dark and light themes must use semantic tokens rather than separate ad hoc CSS.

---

# 17. Shared UI primitives

Implement accessible primitives before individual pages:

```text
Panel
Stat
Gauge
TempBadge
DutyBar
Slider
RangeSlider
Toggle
SegmentedControl
Select
Tabs
Tooltip
ConfirmDialog
Drawer
Toast
EmptyState
Skeleton
StatusDot
Sparkline
IconButton
```

Every interactive control must support:

```text
keyboard operation
focus-visible
ARIA labels
disabled state
error state
loading state
```

Fan sliders should support arrow-key adjustment and expose the exact percentage to assistive technology.

---

# 18. Application shell

Replace `App.tsx` with a proper shell.

Desktop structure:

```text
┌──────────────┬────────────────────────────────────┐
│ Sidebar      │ Global status strip                │
│              ├────────────────────────────────────┤
│ Overview     │                                    │
│ Fans         │                                    │
│ Curves       │            Current page            │
│ Sensors      │                                    │
│ Analytics    │                                    │
│ Automation   │                                    │
│ Settings     │                                    │
│ Diagnostics  │                                    │
└──────────────┴────────────────────────────────────┘
```

The status strip shows:

```text
daemon connection
effective mode
effective profile
hottest temperature
critical state
backend
automation-active indicator
```

Default GTK window:

```text
1280 × 800
```

Below roughly 900 px, collapse the sidebar to icon-only mode.

Persist the user's preferred sidebar state.

---

# 19. Command palette

Implement `Ctrl+K` as the primary keyboard navigation/action interface.

Search domains:

```text
pages
profiles
modes
fans
curves
rules
common commands
```

Examples:

```text
Open Sensors
Set profile: Quiet
Set mode: Auto
Edit CPU Curve
Enable Gaming Rule
Return all fans to EC Auto
```

Preserve convenient direct controls such as:

```text
[ / ] duty adjustment
```

where they remain safe and meaningful.

---

# Phase 3: Product surfaces

## 20. Overview

The Overview page should answer, within a few seconds:

```text
How hot is the machine?
What are the fans doing?
Why are they doing it?
Is anything wrong?
```

Use two primary CPU/GPU instrumentation clusters containing:

```text
temperature
temperature trend
effective requested duty
RPM
current controlling source
thermal state
```

Below that, show a compact fan table/strip.

Each fan row contains:

```text
fan name
RPM
duty
source
5-minute sparkline
effective curve/profile
```

Manual control should be accessible without dominating the default interface.

Add:

```text
active automation summary
latest warning
current policy explanation
```

A useful policy explanation might read:

```text
GPU Fan
72°C GPU → Performance curve → 64%
```

or:

```text
CPU Fan
Critical override → 100%
```

This makes the daemon's reasoning visible without forcing the user into Diagnostics.

Delete the retired:

```text
Hero
ModeBar
FanCard
legacy Sparkline
```

implementations once feature parity exists.

---

# 21. Fans

Create one detailed section per physical fan.

Expose:

```text
name
enabled state
control type
assigned curve
minimum duty
maximum duty
live RPM
live duty
controlling sensor
current requested duty
final duty
```

The important debugging trace should be presented directly:

```text
GPU 71°C
   ↓
Gaming curve
   ↓
Interpolated 63%
   ↓
Fan bounds 20-90%
   ↓
63% written
```

### Fan testing

Implement a daemon-owned temporary test command rather than changing persistent configuration.

For example:

```text
fans.test {
    fan,
    delta: 10,
    duration_ms: 5000
}
```

The daemon records the original effective target and automatically expires the override.

The test layer remains subordinate to thermal safety.

If the application closes mid-test, expiry still occurs because the daemon owns the timer.

---

# 22. Curve Studio

Rebuild the curve editor as a full workspace.

Support:

```text
multiple named curves
simultaneous curve overlays
temperature source selection
fan assignments
duplicate
rename
delete
import
export
```

Keep SVG dragging, but isolate curve mathematics into testable pure functions:

```text
normalizePoints()
interpolateDuty()
insertPoint()
movePoint()
deletePoint()
validateCurve()
```

Display the selected curve strongly and other enabled overlays with reduced prominence.

### Ghost preview

While dragging:

```text
stored curve
      ↓
proposed curve
      ↓
predicted duty at current temperature
```

Show the uncommitted result as a dashed or ghosted preview.

Do not save configuration continuously for every mouse-move event.

Dragging remains local until commit, or saves through a debounced explicit policy if intentionally desired.

### Curve assignment

Fan chips indicate assignment:

```text
CPU Fan
GPU Fan
Chassis Fan
```

Deleting a curve currently assigned to fans requires explicit reassignment or confirmation of fallback behavior.

Never leave dangling references silently.

---

# 23. Sensors

Replace the narrow SensorRail with a complete sensor explorer.

Group by physical source:

```text
CPU / package
GPU
motherboard
EC
NVMe
other hwmon devices
```

Each row displays:

```text
friendly label
hardware identity
live value
60-second trend
source
availability
```

Allow the user to pin meaningful sensors.

Clearly distinguish:

```text
policy aliases such as CPU/GPU
physical hardware sensors
derived "max" readings
```

The page should answer:

```text
Which sensor is currently driving fan behavior?
```

Include a visible hottest-source attribution:

```text
Current hottest source:
GPU Core 76°C
```

Sensors acquired through `nvidia-smi` or another non-hwmon source should be visibly marked.

---

# 24. Analytics

Keep uPlot.

Split analytics into:

```text
Live
Historical
Statistics
```

The live view holds approximately the most recent 30 minutes from memory.

Historical ranges:

```text
1 hour
24 hours
7 days
custom range if inexpensive to support
```

The frontend sends:

```text
since
until
max_points
```

and lets the daemon downsample.

Charts should support plotting selected:

```text
temperature sensors
fan duty
RPM
```

without trying to display every metric simultaneously.

Statistics from `history.stats` include:

```text
average temperature
maximum temperature
average duty
maximum duty
time in duty bands
time per profile
critical events
```

Duty-band metrics should preferably be computed per fan rather than combining unrelated fans into one ambiguous number.

CSV export should use the existing GTK file workflow and export explicit columns with stable timestamps and units.

---

# 25. Automation

The Automation page contains two conceptual sections:

```text
Rules
Schedule
```

The rules table shows:

```text
name
trigger
action
priority
state
enabled
```

Live state should distinguish:

```text
inactive
waiting for sustain
active
shadowed
cooldown
```

The rule editor dynamically exposes fields appropriate to its trigger and action.

Example:

```text
Trigger: Temperature above
Sensor: GPU
Temperature: 75°C
For: 3 ticks

Action: Set profile
Profile: Performance

Priority: 100
Cooldown: 30 seconds
```

`Test Rule` calls `rules.test` and presents the daemon's explanation without applying the action.

The weekly schedule editor displays profile blocks over a week grid, but the underlying storage remains simple structured schedule data rather than storing visual geometry.

---

# 26. Settings and Safety

Use sections rather than one very long settings form.

### Safety

Expose:

```text
critical temperature
normal global noise cap
hysteresis
firmware fallback state
```

Clearly communicate that critical thermal protection can override noise limits.

Provide a prominent:

```text
Return to EC Auto
```

control.

This action should require confirmation if it materially changes active control behavior.

### Appearance

Expose:

```text
dark/light/system theme
sidebar preference
°C / °F
```

Keep all internal daemon temperatures in Celsius.

°F is presentation only.

This avoids temperature conversions leaking into policy configuration and comparison logic.

### Notifications

Expose:

```text
desktop alerts
critical alerts
optional temperature thresholds
```

### Daemon

Display:

```text
daemon version
protocol version
configuration schema
backend
control interval
configuration path
history status
```

### About

Use the project's canonical version source rather than duplicating a manually maintained UI version.

---

# 27. Diagnostics

Diagnostics must become an actual engineering surface rather than a text dump.

Sections:

```text
Daemon
Backend
Capabilities
Sensors
Configuration
History
Recent Decisions
Warnings
```

The Recent Decisions view should expose the daemon's decision ring buffer.

Example:

```text
08:21:14.502
GPU Fan
GPU Core 74°C
curve=gaming
requested=67%
bounded=67%
final=67%
write=yes
```

Another example:

```text
08:21:15.003
GPU Fan
requested=68%
final=68%
write=no
reason=hysteresis
```

Include:

```text
Copy diagnostics
Export diagnostics
```

Export should redact or omit unnecessary system-identifying information by default.

Critical and backend banners are rendered globally where relevant, with full details available here.

---

# Phase 4: Tray and CLI

## 28. Tray integration

Expand `TrayApplication` without turning the tray into another full interface.

Display:

```text
current hottest temperature
current effective duty/profile
```

Add:

```text
profile radio items
mode radio items
open dashboard
return to EC auto
```

Use a slower poll interval than the foreground dashboard.

Critical state should alter the tray icon or status representation.

The tray must consume the same daemon API rather than implement independent policy logic.

---

# 29. CLI

Expand `fan-ctl.py` around the new RPC.

Examples:

```text
fan-ctl fans list
fan-ctl fans configure fan1 ...
fan-ctl fans test fan1 ...

fan-ctl curves list
fan-ctl curves assign ...

fan-ctl rules list
fan-ctl rules test ...
fan-ctl rules enable ...
fan-ctl rules disable ...

fan-ctl history stats
fan-ctl history query ...

fan-ctl sensors list
fan-ctl diagnostics
```

Preserve:

```text
--json
```

Every CLI command should use the same RPC validation paths as the UI.

Avoid implementing separate config-file mutation logic in the CLI.

---

# Phase 5: Cleanup and consolidation

Once feature parity is reached, remove retired architecture rather than leaving two systems indefinitely.

Delete obsolete components including:

```text
Hero
ModeBar
FanCard
SensorRail
HistoryBoard
old CurveStudio implementation
old SafetyDrawer implementation
obsolete Banners implementation
```

Re-home surviving logic rather than copying it.

Remove unused CSS and old style tokens.

Remove deprecated:

```text
--port
--no-browser
```

options from `fan-gui.py` if they are genuinely no longer supported.

Search the repository for dead references before deletion.

Update:

```text
ROADMAP.md
SECURITY.md
README.md
metainfo
screenshots
packaging documentation
```

Document the history database location and privileges in `SECURITY.md`.

Document automation command execution if `run_command` ships.

---

# Phase 6: GTK and desktop integration polish

Keep the existing bridge protocol where practical.

Change the default window size to:

```text
1280 × 800
```

Ensure:

```text
minimum usable dimensions
sidebar collapse
WebKit zoom behavior
keyboard shortcuts
clipboard
file dialogs
```

work correctly under the actual WebKitGTK host, not only in Chromium during frontend development.

Test application startup with:

```text
daemon already running
daemon unavailable
daemon starting slowly
daemon restarting while GUI is open
incompatible daemon protocol
```

A daemon restart should not require restarting the GUI.

---

# Phase 7: Verification

## Python gates

Run:

```text
make test
ruff check
scripts/check_versions.py
```

The target is not an arbitrary test count. The requirement is meaningful coverage of every new control path.

Do not optimize implementation around reaching "110+ tests" if 95 high-quality tests provide better coverage than 120 superficial tests.

## Frontend gates

Run:

```text
npm test
npm run build
```

Add focused Vitest coverage for:

```text
store selectors
connection/staleness behavior
curve interpolation
curve preview
route loading
automation editor transformations
RPC error handling
temperature conversion
```

Use component tests for behavior rather than snapshot-testing the entire application.

---

# 30. End-to-end daemon simulation

Extend the headless test mode into a deterministic simulated system.

A scripted scenario should be capable of producing:

```text
CPU warming
GPU warming
rule activation
profile change
fan ramp
critical-temperature override
temperature recovery
rule cooldown
fan ramp-down
```

Then assert the resulting decisions.

This is considerably more valuable than only verifying that RPC methods return successfully.

Example scenario:

```text
GPU 55°C
    ↓
GPU 68°C
    ↓
GPU 76°C for 3 ticks
    ↓
Gaming rule activates
    ↓
Performance profile becomes effective
    ↓
GPU fan reaches expected curve duty
    ↓
GPU reaches critical threshold
    ↓
Safety forces 100%
    ↓
GPU recovers
    ↓
Safety releases
    ↓
Rule/profile control resumes
```

This tests the complete architecture as one system.

---

# 31. Headless GUI smoke testing

Extend `--headless-smoke` to verify that every route can initialize:

```text
Overview
Fans
Curves
Sensors
Analytics
Automation
Settings
Diagnostics
```

Smoke testing should also validate:

```text
bridge initialization
capability negotiation
snapshot retrieval
live update delivery
representative mutation
daemon reconnect
```

The test does not need to prove visual correctness.

---

# 32. Visual acceptance

Capture deterministic screenshots of every primary page using representative fake telemetry.

Use the same fixture data each run.

Review:

```text
hierarchy
density
spacing
alignment
typography
visual consistency
light mode
dark mode
warning state
critical state
empty state
stale state
disconnected state
```

Do not evaluate only the ideal connected/dark-mode screenshot.

Visual polish should include failure states because hardware utilities spend meaningful time displaying abnormal conditions.

---

# 33. Packaging

Create the runtime directory through tmpfiles:

```text
/var/lib/fan-control
```

with:

```text
0750 root:fan-control
```

Verify package ownership and daemon permissions.

Python's standard-library `sqlite3` module does not itself imply that every distribution's Python build necessarily includes working SQLite support, so packaging/CI should verify:

```python
import sqlite3
```

rather than automatically adding an unnecessary standalone `sqlite3` CLI dependency.

Update:

```text
Debian packaging
RPM spec
PKGBUILD
installed file lists
tmpfiles configuration
```

as appropriate.

Ensure the built:

```text
ui/dist
```

bundle is refreshed as part of the release process rather than relying on developers to remember manually.

---

# 34. Release compatibility

Before release, test these upgrade paths:

```text
v1 package + v1 config
v2 package + fresh config
v2 package + migrated v1 config
v2 GUI reconnecting to v2 daemon
v2 CLI against v2 daemon
```

Where practical, test behavior against an older daemon.

The frontend should detect missing capabilities and communicate:

```text
This daemon does not support Automation.
```

rather than crashing because `rules.list` does not exist.

---

# 35. Explicit failure modes

The design should intentionally define behavior for:

```text
configuration unreadable
configuration partially invalid
history DB unwritable
history DB corrupt
sensor disappears
GPU command unavailable
fan tach unavailable
backend write fails
daemon reconnect
fan curve deleted while assigned
rule references missing sensor
schedule overlap
clock/timezone change
frontend sends stale revision
live stream freezes
```

Every failure should fall into one of three categories:

```text
safe fallback
feature degradation
hard failure
```

Fan control should continue whenever a safe fallback exists.

Diagnostics must explain which fallback is active.

---

# 36. Explicit non-goals for v2

Keep the redesign controlled by documenting what is not included.

Unless separately approved, v2 does not attempt:

```text
Uniwill RPM/tach support requiring unverified hardware work
remote network control
cloud synchronization
mobile applications
kernel-driver replacement
automatic fan calibration
arbitrary plugin execution
cross-machine history synchronization
```

These should not be allowed to expand the critical path of the redesign.

---

# 37. Phase completion gates

## Gate A: Daemon v2

Complete when:

```text
v2 config works
v1 migrates safely
per-fan policy works
rules work
schedule works
persistent telemetry works
RPC v2 works
legacy behavior remains functional
tests pass
```

The old UI still functions well enough to control the daemon.

## Gate B: Frontend platform

Complete when:

```text
new shell launches in WebKitGTK
hash routing works
capability negotiation works
snapshot/live architecture works
reconnection works
design primitives exist
```

Do not build all pages before proving the host architecture.

## Gate C: Core control UI

Complete when:

```text
Overview
Fans
Curve Studio
Sensors
```

fully replace their legacy counterparts.

At this point the new UI is usable for normal fan management.

## Gate D: Extended product UI

Complete when:

```text
Analytics
Automation
Settings
Diagnostics
```

are functional.

## Gate E: Integration

Complete when:

```text
tray
CLI
exports
documentation
packaging
cleanup
```

are complete.

## Gate F: Release candidate

Complete only when:

```text
Python tests pass
frontend tests pass
build passes
lint passes
version checks pass
headless daemon simulation passes
GTK smoke passes
packaging passes
visual acceptance passes
```

---

# 38. Recommended implementation sequence

The implementation order should be:

```text
Configuration schema + migration
        ↓
Policy/control-loop refactor
        ↓
Stable sensor identities
        ↓
Per-fan control
        ↓
Rules + schedule
        ↓
History
        ↓
RPC formalization
        ↓
Fake backend + complete daemon tests
        ↓
Frontend bridge/store architecture
        ↓
Design system + application shell
        ↓
Overview
        ↓
Fans
        ↓
Curve Studio
        ↓
Sensors
        ↓
Analytics
        ↓
Automation
        ↓
Settings
        ↓
Diagnostics
        ↓
Tray + CLI
        ↓
Legacy cleanup
        ↓
Packaging/docs
        ↓
End-to-end and visual verification
```

This order minimizes the number of temporary compatibility layers required during development.

---

# 39. Primary engineering risks

### Configuration migration

This is the highest data-integrity risk.

Mitigation consists of deterministic migration, schema validation, atomic persistence, pre-migration backup, fixture-based migration tests, and preserving the original file on failure.

### Thermal safety regressions

This is the highest functional risk.

Safety logic must remain daemon-owned and must be tested independently of UI functionality.

Critical protection must outrank:

```text
rules
schedules
manual control
noise caps
fan tests
```

### Sensor identity instability

Persisting `hwmonX` indices would cause curves and rules to silently target incorrect or missing sensors across boots.

Use stable hardware/device identities and treat runtime hwmon numbers only as metadata.

### Automation ambiguity

Rules that directly mutate persistent mode/profile state become difficult to reason about.

Use transient overlays with explicit priority and expose both configured and effective states.

### RPC/UI state divergence

Multiple control surfaces can race.

Use configuration revisions and authoritative mutation responses.

### History growth or corruption

Bound storage through retention, avoid blocking the control loop on SQLite, batch writes, and degrade gracefully to memory-only telemetry.

### WebKit-specific frontend problems

Routing, keyboard input, SVG rendering, file dialogs, and CSS can behave differently from development Chromium.

Verify the real `fancontrol://` WebKitGTK environment at the beginning of the frontend phase, not at the end.

### `run_command`

This can accidentally turn a fan-control daemon into a privileged arbitrary-command executor.

Ship it only with an explicit command allowlist, no shell invocation, and secure daemon-side enforcement.

---

# 40. Definition of done

fan-control v2 is complete when it feels like one coherent system rather than a collection of newly added pages.

A user must be able to determine:

```text
what every fan is doing
why it is doing it
which temperature caused it
which curve/rule/profile produced the request
whether safety modified that request
what happened historically
whether the daemon is functioning correctly
```

without reading raw daemon output.

At the architectural level, every hardware write must be explainable from a recorded control decision, every persistent mutation must pass through validated daemon-owned configuration, every automation effect must have deterministic precedence, and loss of optional subsystems such as analytics or the GUI must never compromise basic fan control.

The final product should preserve the reliability and hardware authority of the existing daemon while replacing the current dashboard with a dense, responsive, keyboard-friendly control workstation designed around real-time observability and precise control rather than cosmetic dashboard widgets.