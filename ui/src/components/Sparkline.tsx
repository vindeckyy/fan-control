type Props = { values: Array<number | null> };

export default function Sparkline({ values }: Props) {
  const pts = values.filter((v): v is number => v != null);
  if (pts.length < 2) {
    return <svg className="spark" aria-hidden="true" />;
  }
  const min = Math.min(...pts, 30);
  const max = Math.max(...pts, 90);
  const d = pts
    .map((value, i) => {
      const x = (i / (pts.length - 1)) * 120;
      const y = 36 - ((value - min) / (max - min || 1)) * 32;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="spark" viewBox="0 0 120 40" aria-hidden="true">
      <path d={d} fill="none" stroke="var(--amber)" strokeWidth="2" />
    </svg>
  );
}
