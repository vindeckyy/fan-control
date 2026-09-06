import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useConfig } from "../../stores/configStore";
import { temperature } from "../../temperature";

export function Panel({
  title,
  actions,
  children,
  className = "",
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <header className="panel-heading">
          <h2>{title}</h2>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}
export function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}) {
  return (
    <div className="stat">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}
export function TempBadge({ value }: { value?: number | null }) {
  const unit = useConfig((s) => s.config?.display.temperature_unit || "c");
  const critical = useConfig((s) => s.config?.safety.critical_temp || 95);
  return (
    <span
      className={
        value != null && value >= critical
          ? "critical"
          : value != null && value >= critical - 15
            ? "warning"
            : ""
      }
    >
      {temperature(value, unit)}
    </span>
  );
}
export function DutyBar({
  value,
  label = "Duty",
}: {
  value?: number | null;
  label?: string;
}) {
  return (
    <div className="duty">
      <span>
        {label} <b>{value == null ? "Unavailable" : `${value}%`}</b>
      </span>
      <meter aria-label={label} min={0} max={100} value={value || 0} />
    </div>
  );
}
export function Slider({
  label,
  value,
  onChange,
  min = 0,
  max = 100,
  disabled = false,
  unit = "%",
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  disabled?: boolean;
  unit?: string;
}) {
  return (
    <label className="field">
      <span>
        {label}{" "}
        <b>
          {value}
          {unit}
        </b>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        aria-valuetext={`${value}${unit}`}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}
export function Toggle({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="toggle">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}
export function Select({
  label,
  value,
  onChange,
  children,
  disabled = false,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      >
        {children}
      </select>
    </label>
  );
}
export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
export function Skeleton({
  label = "Reading daemon state…",
}: {
  label?: string;
}) {
  return (
    <p className="empty" role="status">
      {label}
    </p>
  );
}
export function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null),
    id = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    ref.current?.showModal();
    return () => {
      ref.current?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={id}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <header className="panel-heading">
        <h2 id={id}>{title}</h2>
        <button type="button" onClick={onClose} aria-label="Close dialog">
          Close
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function ConfirmDialog({
  label,
  title,
  children,
  onConfirm,
  disabled = false,
}: {
  label: string;
  title: string;
  children: ReactNode;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" disabled={disabled} onClick={() => setOpen(true)}>
        {label}
      </button>
      {open && (
        <Dialog title={title} onClose={() => setOpen(false)}>
          <p>{children}</p>
          <div className="actions">
            <button type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => {
                onConfirm();
                setOpen(false);
              }}
            >
              {label}
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}
export function Sparkline({
  values,
  label,
}: {
  values: (number | null | undefined)[];
  label: string;
}) {
  const valid = values.filter((x): x is number => x != null);
  if (valid.length < 2) return <small>Collecting {label.toLowerCase()}…</small>;
  const min = Math.min(...valid) - 2,
    span = Math.max(5, Math.max(...valid) - min + 2);
  return (
    <svg
      className="sparkline"
      viewBox="0 0 300 55"
      role="img"
      aria-label={label}
      preserveAspectRatio="none"
    >
      <polyline
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        vectorEffect="non-scaling-stroke"
        points={valid
          .map(
            (v, i) =>
              `${(i * 300) / (valid.length - 1)},${55 - ((v - min) / span) * 55}`,
          )
          .join(" ")}
      />
    </svg>
  );
}
