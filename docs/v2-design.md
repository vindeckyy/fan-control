# v2 design decisions

Direction comes from the user's v2 plan and the agreed design in HANDOFF.md.
Antislop applies during implementation, as requested by the user.

ENERGY 1 / RHYTHM 2 / MOTION 1.

- Near-black layers reduce visual interference around continuously changing
  readings. Light and system preferences use the same semantic tokens.
- Cyan identifies selected controls and the primary telemetry line. Amber and
  red identify warning and critical states, always with explanatory text.
- System typography matches the native desktop environment. Tabular numerals
  keep temperatures and fan speeds aligned as they change.
- CPU/GPU clusters answer the overview's immediate thermal question. Fan
  forms, the curve plot, sensor tables, and the weekly schedule use different
  layouts because each supports a different editing or inspection task.
- Small panel radii distinguish groups; squared control boundaries identify
  input areas. Borders separate surfaces without decorative shadows.
- Curve-grid lines encode duty values. Dashed previews identify unsaved edits.
  Sparkline and history lines represent daemon telemetry, never invented data.
- Persistent navigation follows the eight pages requested in the plan.
  Narrow layouts retain their labels and stack control sections.
- Native inputs and dialogs provide keyboard operation, focus containment,
  exact numerical values, and Escape dismissal. Motion is limited to normal
  control feedback.

Acceptance screenshots use explicitly labeled simulated hardware.
