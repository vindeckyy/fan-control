# HANDOFF — fan-control v2 redesign (in progress)

Date of handoff: 2026-09-05. Branch: `main`, working tree has large uncommitted changes (do not commit unless the user asks).

## 1. Mission

Execute the plan in the user's pasted document: *"fan-control v2: Full Product, UI, Daemon, and Architecture Redesign"*. The original paste lives at
`/home/hayden/.zcode/tmp/paste-attachments/2026-09-05/pasted-text-20260905-082138-492b3ca0.txt`
(tmp dir — copy it into the repo, e.g. `docs/v2-plan.md`, before it gets cleaned). It defines 7 phases and gates A–F. Section 38 gives the implementation order; section 40 the definition of done.

**Hard constraint discovered early:** the existing test file `test_fan_control.py` (1868 lines, 110 tests) pins legacy internals — `controller.state[...]` dict access, profile↔curve sync, `_missing`/`_released_for_fault` attributes, legacy RPC shapes, GTK header keys. All of these must keep passing. Therefore v2 was built as: **v2 config document is canonical; legacy surfaces are adapters over it.**

## 2. Architecture as implemented

### Data flow
```
fan-daemon.py ──> FanController (fan_controller.py)
                    ├─ self.config  : canonical v2 document (dict, has "version": 2, "revision")
                    ├─ self.state   : LegacyStateView — read/write v1-flat adapter over config
                    ├─ tick_*()     : staged pipeline per plan §2
                    ├─ handle()     : legacy RPC + v2 RPC dispatch (all methods, revision-checked)
                    └─ HistoryStore (fan_history.py, SQLite, degrading)
fan_engine.evaluate(config, context, runtime)  — PURE staged pipeline (no I/O)
fan_rules — rules engine + schedule evaluator (pure, transient overlays only)
fan_policy — v2 schema/migration/validation/atomic save + legacy helpers + APP_VERSION="2.0.0"
```

### v2 config schema (in `fan_policy.py`)
- Sections: `version, revision, mode(auto|manual|released), profile, safety{critical_temp, global_max_duty, hysteresis, firmware_fallback}, display{theme, temperature_unit}, notifications{enabled, critical_temp}, sensors{cpu_pin, gpu_pin, pinned[]}, fans{fan1..fan3}, curves{}, rules[], schedule{}, history{persist, retention_days}`.
- Fan: `{name, enabled, control:{type: linked|profile|curve|manual, curve_ref?, target?, temp_source?}, min_duty, max_duty}`. **Note:** control type `profile` was added beyond the plan's three types — it is produced by migration for "legacy unlinked fan with no per-side curve" (follows global profile curve at its side's temperature). Without it, legacy behavior diverges.
- Curves: system ids `legacy_custom` (v1 shared curve), `cpu_default`, `gpu_default`; user curves `named_<sanitized>` with deterministic collision suffixes. Built-in profiles (silent/balanced/performance) are code-owned in `PROFILES`, never stored in config.
- Mode mapping legacy↔v2: `curve→auto`, `manual→manual`, `released→released`.
- Key functions: `default_config_v2, detect_config_version, upgrade_config_v1_to_v2, validate_config, normalize_config(doc)->(doc,warnings), migrate_document, load_document(path)->(doc,warnings,migrated,original_version), save_document(path,doc,original_version=)` (atomic tmp+fsync+rename; backs up `*.v1.backup.json` before first v1→v2 write, never clobbers backup), `ConfigError(ValueError)` with `.code="CONFIG_ERROR"`.
- `scan_sensor_records()` returns stable-ID sensor records: `hwmon:<chip>:<device-identity>:<channel>` (device identity from sysfs device symlink, e.g. `platform-nct6775.656`, `pci-0000:05:00.0`), `nvidia:<pci-bus-id>` for nvidia-smi (4-field query). Records carry BOTH `value` and `temp` keys (legacy pickers read `temp`). Legacy `scan_sensors()` untouched.
- Unknown top-level config keys are retained; `_V1_CONSUMED_KEYS` lists what migration eats.

### Controller (fan_controller.py)
- `LegacyStateView` maps legacy keys ↔ v2 (mode, profile, curve↔legacy_custom/profile curve, curve_cpu/gpu↔cpu_default/gpu_default, targets↔per-fan `control.target`, max_duty/hysteresis/critical_temp→safety.*, theme, alerts, cpu_sensor/gpu_sensor→sensors pins, linked↔fan1+fan2 both `linked` type). View mutations are in-memory only (matches legacy).
- Legacy `set` RPC sets per-fan `control.target` + mode manual (does NOT rewrite control types — keeps `linked` view stable); `custom` RPC maps which=cpu/gpu/shared → cpu_default/gpu_default/legacy_custom + profile custom, and flips `profile`-type fans to the curve.
- All mutating handlers: `_require_revision(params)` (honors `expected_revision`, raises `RpcError(REVISION_CONFLICT)`) then `_commit(changed)` → bump revision + `save_document` + reply `{ok, config_revision, changed}`.
- New RPC: `capabilities, fans.list/rename/configure/test, curves.list/set/assign/delete(id+force; legacy name path keeps force=True), sensors.list, rules.list/set/delete/test, schedule.get/set, history.query/stats, diagnostics.snapshot/decisions`. `fans.test` stores `_overrides[fan_id] = {duty, expires}` (epoch-based; daemon-owned expiry).
- `tick_control()`: builds context (cpu/gpu via `self.cpu_temp()/self.gpu_temp()` — tests patch these), calls engine, updates `_rule_state`, handles fault-release/recovery exactly like legacy decide() did, writes, records traces into `_decisions` deque (600). Returns `TickResult` exposing legacy attrs (`.action .writes .duties .critical .missing_temp`) + `.engine`.
- `live_snapshot()`: legacy keys + `seq, timestamp, config_revision, effective_mode/profile, policy_source, active_rules, configured_mode/profile, backend_state`.
- Default data dir: `/var/lib/fan-control` (env `FAN_CONTROL_DATA_DIR`, `--data-dir` flag; demo→runtime dir). History failure degrades, never crashes (non-root test proved this).

### Engine (fan_engine.py)
Pure `evaluate(config, context, runtime)`; precedence chain implemented: safety(critical→100, bypasses caps/bounds/overrides/rules) > temp overrides > rule overlay > schedule overlay > configured mode/profile > per-fan control (linked→hottest+profile curve, profile→side temp+profile curve with cross-side fallback, curve→curve_ref+its temp_source, manual→target·cap/100) > fan min/max bounds > hysteresis (vs readback duty; critical always writes). Missing temps in auto → idle, release after 3 (`firmware_fallback`). Decision trace per fan with all §2 fields.

### Rules/schedule (fan_rules.py)
- States: inactive/sustaining/active/cooldown/applied/shadowed. Overlays come from ALL active rules each tick (not just transition); arbitration by (safety_class desc, priority desc, config order asc) per output key. Cooldown gates re-fire. `on_startup` fires once per daemon run. Triggers: temp_above/below, rpm_below, time_range (midnight-crossing: window on day D covers D start→D+1 end; implementation checks `today in days and min>=start` OR `yesterday in days and min<end` when start>end), on_startup. Actions: set_profile, set_mode, set_duty (overlay keys `fan{id}`), notify, run_command (allowlist enforcement NOT yet implemented daemon-side — plan §6.4; commands currently only surfaced in engine result).
- `evaluate_schedule`: enabled/timezone(zoneinfo via `local_now`)/items, last-entry-wins on overlap.

### RPC transport (fan_rpc.py)
- `RpcError(message, code)`; codes: INVALID_ARGUMENT, NOT_FOUND, REVISION_CONFLICT, UNSUPPORTED, BACKEND_ERROR, PERMISSION_DENIED, CONFIG_ERROR, HISTORY_UNAVAILABLE. Server replies `{"ok":false,"error":{"code","message"}}` for RpcError, plain string otherwise (legacy). Client raises `RpcError` (subclass of RuntimeError — legacy `assertRaises(RuntimeError)` still passes).
- `fan_gtk.py`: `INJECT` JS converts `__fan_error` objects → `Error` with `.code`; `_on_message` catches RpcError → returns `{"__fan_error": {...}}` as result. `READONLY_METHODS` frozenset guards push_full. Window now 1280×800 default, min 720×480.

## 3. Current state (verified)

- **Legacy suite: 110/110 PASS.** `ruff check .` clean.
- **New suite `test_fan_control_v2.py`: 88 tests, 77 pass, 11 fail (7 FAIL / 4 ERROR).**
- Load order in v2 test file matters (importlib `load()` + sys.modules identity): `policy, backend, rpc, rules, engine, history, controller`. Do not reorder — `RpcError`/`FanBackendError` identity across modules depends on it.
- Everything is UNCOMMITTED. `git status` shows modified: fan_policy, fan_controller, fan_rpc, fan_gtk, fan-daemon; new: fan_engine, fan_rules, fan_history, test_fan_control_v2.

## 4. The 11 remaining v2 test failures — root causes already diagnosed

Fix source first, then tests; do not just adjust expectations where the source is wrong.

**Source bugs (fix these):**

1. **Engine drops fan-loop warnings** (`test_missing_curve_falls_back_to_safe_default_with_warning`). In `fan_engine.evaluate`, `result["warnings"]` is built as `warnings + rule_result["warnings"]` BEFORE the per-fan loop; the loop then appends to the local `warnings` var (missing-curve fallback, `_curve_temp` unavailable-source) which is never re-read. Fix: append to `result["warnings"]` in the loop and inside `_curve_temp` (pass `result["warnings"]` or return warnings and extend).

2. **`_bucket_average` off-by-one** (`test_query_filters_and_downsamples`): last point's bucket index equals `max_points` → 51 buckets for max 50. Fix in `fan_history._bucket_average`: `buckets.setdefault(min(int(offset), max_points - 1), ...)`.

3. **ValueError leaks as plain string errors** (`test_rules_test_rejects_invalid_payload`, `test_schedule_get_set`, `test_v2_args_validated_over_socket`). In `fan_controller`: `_rules_set` must wrap `validate_rule_payload` (raises ValueError) → `RpcError(str(e), "INVALID_ARGUMENT")`; `_schedule_set` must catch `policy.ConfigError` from `normalize_config` → `RpcError(str(e), "CONFIG_ERROR")`; `_history_query` must wrap `int()` conversions of `max_points`/`since`/`until` → RpcError INVALID_ARGUMENT (and `_fan_id` in the fans list already raises RpcError).

**Test bugs (fix the tests):**

4. **`temp_record` sets both `value` and `temp` keys, but tests mutate only `value`** — legacy pickers (`pick_gpu_temp` etc.) read `temp`, so the controller never sees the change. This explains ALL THREE scenario failures: `test_rules_lifecycle_and_test` (sets value=76, temp stays 55 → matched False), `test_rule_activation_changes_effective_profile` (rule never fires → 'balanced'), and both EndToEnd failures (gpu "warming" never visible → first tick at 45°C→duty 0→action 'idle'). Fix: make the e2e `set_gpu()` (and the lifecycle test) set BOTH keys, e.g. `rec["temp"] = rec["value"] = value`. (Alternative: change pickers to read `value` — do NOT, legacy dicts only have `temp`.)

5. `test_schedule_get_set`: uses profile `"quiet"` which doesn't exist (PROFILES = silent/balanced/performance/custom) → normalize raises. Use `"silent"` (plus item #3 above makes the negative case return RpcError).

6. `test_write_failure_does_not_crash_control_loop`: IndexError on `backend.writes[-1]` because controller default mode is `manual` with targets 0 → duties all 0 → no writes. Add `self.ctl.handle("mode", {"mode": "curve"})` in the test, and compare against `result.duties` correctly (writes list contains both fans; filter by fan 1).

7. `test_named_curve_collision_resolves_deterministically`: `sanitize_curve_id("a-b")` → `named_a-b` vs `"a_b"` → `named_a_b` — no collision. Use names `"a b"` and `"a_b"` (both sanitize to `named_a_b`) to force the `_2` suffix.

**After these fixes:** re-run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest` (both files) and `ruff check .`.

## 5. Remaining Phase 1 tasks (before Gate A)

1. Makefile `install:` — add `fan_rules.py fan_engine.py fan_history.py` to the `install -m644` LIBDIR line.
2. `.github/workflows/ci.yml` — add the three new files to the `py_compile` list; add a step `python -c "import sqlite3"` (plan §33).
3. `packaging/` — check PKGBUILD/spec/debian for hardcoded python-file lists; add `packaging/tmpfiles.conf` entry `d /var/lib/fan-control 0750 root fan-control -` (keep existing /run entry); bump versions to 2.0.0 in PKGBUILD `pkgver`, spec `Version:`, debian changelog, metainfo `<release version=>` so `scripts/check_versions.py` stays green.
4. Optional but planned: `run_command` daemon enforcement (allowlist file, no shell, timeout) — engine already emits `commands` requests; controller ignores them today. Ship disabled-by-default per §6.4 or document as not-shipped.
5. Gate A checklist (§37): v2 config works ✓, v1 migrates ✓, per-fan policy ✓, rules ✓, schedule ✓, telemetry ✓, RPC v2 ✓, legacy functional ✓, tests pass ← finish this.

## 6. Frontend plan (Phases 2–3) — agreed design

- Add deps: `react-router-dom` (HASH routing only — custom `fancontrol://` scheme, no HTTP server), `zustand`. Keep react 18, uplot, vite 6, vitest 2, jsdom.
- Structure under `ui/src/`:
  - `theme/tokens.css` — two-layer tokens: primitives (`--c-bg-0..3, --c-accent, --c-warning, --c-danger, --space-*, --radius-*, --font-size-*`) → semantic (`--surface-app/card/raised, --border-subtle, --text-primary/secondary, --reading-normal/hot/critical`). Dark-first instrumentation look: near-black layers, subtle borders, cyan/teal accent, amber warning, red critical, 13px dense type, tabular numerals, no gradients/glow. Light theme via `[data-theme="light"]` overriding semantic tokens only.
  - `client.ts` typed daemon client wrapping `bridge.ts`; errors carry `.code`; all pages go through client/stores, never the raw bridge.
  - `stores/`: `connectionStore` (status: connected/reconnecting/stale/unsupported-version/daemon-error; staleness if last live >5s — de-emphasize values, never zero), `liveStore` (high-freq telemetry), `configStore` (snapshot/config, revision-aware mutations: send `expected_revision`, apply authoritative response, reconcile via full `snapshot` after multi-object mutations), `uiStore` (sidebar, selected curve, dialogs, °C/°F display-only).
  - `components/ui/` primitives: Panel, Stat, Gauge, TempBadge, DutyBar, Slider, RangeSlider, Toggle, SegmentedControl, Select, Tabs, Tooltip, ConfirmDialog, Drawer, Toast, EmptyState, Skeleton, StatusDot, Sparkline, IconButton — all keyboard-operable, focus-visible, ARIA, disabled/error/loading states; fan sliders expose exact % to AT, arrow keys.
  - `App shell`: sidebar (Overview/Fans/Curves/Sensors/Analytics/Automation/Settings/Diagnostics) + status strip (connection, effective mode/profile, hottest temp, critical, backend, automation-active). Collapse to icons <900px; persist preference. Command palette `Ctrl+K` (pages, profiles, modes, fans, curves, rules, commands); keep `[`/`]` duty adjust.
- Routes `/#/overview` etc. Pages per plan §20–27: Overview (two CPU/GPU clusters + fan strip + policy explanation "GPU 72°C → Performance curve → 64%"), Fans (per-fan debug trace + `fans.test`), Curve Studio (multi-curve overlays, ghost preview while dragging, pure curve math: extend existing `curveMath.ts` with insert/move/deletePoint/validateCurve), Sensors (grouped explorer, pinning, hottest attribution), Analytics (uPlot; live 30min in-memory + `history.query` historical with daemon downsampling + `history.stats`; CSV via GTK file dialog `history.pick_export`), Automation (rules table with live states, dynamic editor, `rules.test` explanations; weekly schedule grid), Settings (Safety/Appearance/Notifications/Daemon/About sections; About uses `capabilities.version`), Diagnostics (sections + decision ring buffer viewer + copy/export).
- GTK side: `_serve_ui` already serves any file under dist root; hash routing needs only index.html. Live push loop (500 ms) already emits `live` events with seq/timestamp. Reconnect: RpcClient retries once per call; GTK `_push_snapshot` flags "Daemon unreachable" — frontend must also auto-recover (re-subscribe capabilities→snapshot→live when events resume).

## 7. Phases 4–7 quick reference

- **Phase 4:** Tray (`TrayApplication` in fan_gtk.py): keep display-only, but add profile/mode radio items + "Return to EC auto" through same RPC (`profile`/`mode` methods), tooltip with hottest temp + effective profile, slower poll, critical changes icon. CLI `fan-ctl.py`: add `fans list/configure/test`, `curves list/assign`, `rules list/test/enable/disable`, `history stats/query`, `sensors list`, `diagnostics` subcommands — all via RpcClient, keep `--json`, no direct config-file writes.
- **Phase 5:** delete `ui/src/components/{Hero,ModeBar,FanCard,SensorRail,HistoryBoard,SafetyDrawer,Banners,Sparkline,Toast}.tsx` + old `CurveStudio` once new pages reach parity; remove `--port`/`--no-browser` from fan-gui.py (they're hidden no-ops); grep for dead refs; update README.md, ROADMAP.md, SECURITY.md (history DB location/privileges, run_command if shipped), metainfo, screenshots.
- **Phase 6:** headless smoke (`fan-gui.py --headless-smoke`) — extend to call capabilities, snapshot, live, one mutation, diagnostics.decisions, history.query against demo controller; route smoke via vitest+jsdom rendering each route with a fake bridge.
- **Phase 7 (Gates E/F):** `make test` (unittest + `cd ui && npm test`), `ruff check .`, `scripts/check_versions.py`, `npm run build`; e2e sim already in v2 tests (extend if needed); visual acceptance via the browser-use skill: build UI, serve `ui/dist` statically or use a demo harness page that injects a `window.__fanControl` mock with fixture data, screenshot all 8 routes + light/warning/critical/stale/disconnected states; packaging verification; release-compat matrix (v1 pkg+v1 cfg, v2 fresh, v2+migrated, GUI/CLI reconnect, missing-capability degradation — frontend must show "This daemon does not support Automation" not crash).

## 8. Gotchas

- Python 3.10 compat required (CI matrix 3.10/3.12/3.13); local interpreter is 3.14. No 3.12+ syntax.
- Tests run from repo root; the importlib `load()` pattern registers `sys.modules` — keep the load order noted in §3 when adding modules to the v2 test file.
- ruff: line-length 100, select E,F,W,I,B,UP, ignore E402,E501, exclude `ui/`.
- `fan_policy.migrate_config` (in fan_backend, duty rescale) is DIFFERENT from the new `migrate_document` — don't confuse them.
- `fan_policy._normalize_rule` is imported by the controller (private cross-import, intentional).
- The GTK tests in the legacy file only run when `gi` is importable; locally it is available, so GTK code paths are exercised — keep `fan_gtk.py` importable and its method shapes intact (e.g. `_update_header` dict keys, `_InProcessAdapter`).
- `HistoryStore` must never raise into the control loop (already proven by the non-root test). Keep that property when touching it.
- Do not commit anything unless the user asks.
