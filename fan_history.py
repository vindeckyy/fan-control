#!/usr/bin/env python3
"""Persistent telemetry history (SQLite, standard library only).

Normalized schema with batched writes. A database failure must never halt fan
control: the store degrades to in-memory telemetry, surfaces a warning and
retries later.
"""

from __future__ import annotations

import collections
import copy
import datetime
import functools
import os
import pathlib
import sqlite3
import threading
import time

FLUSH_EVERY = 10
FLUSH_INTERVAL = 30.0


def synchronized(method):
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def isolated_read(method):
    """Queries use their own WAL reader and never hold the writer lock."""
    @functools.wraps(method)
    def read(self, *args, **kwargs):
        with self._lock:
            snapshot = copy.copy(self)
            snapshot.memory = collections.deque(self.memory, maxlen=MEMORY_RING)
            snapshot._buffer = collections.deque(maxlen=MEMORY_RING)
            snapshot._lock = threading.RLock()
            available = self._conn is not None
        snapshot._conn = None
        try:
            if available:
                snapshot._conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
                snapshot._conn.row_factory = sqlite3.Row
            return method(snapshot, *args, **kwargs)
        except sqlite3.Error as exc:
            if snapshot._conn is not None:
                try:
                    snapshot._conn.close()
                except sqlite3.Error:
                    pass
                snapshot._conn = None
            snapshot.degraded = True
            snapshot.error = f"history query failed: {exc}"
            with self._lock:
                self.degraded = True
                self.error = snapshot.error
            return method(snapshot, *args, **kwargs)
        finally:
            if snapshot._conn is not None:
                try:
                    snapshot._conn.close()
                except sqlite3.Error:
                    pass
    return read
MEMORY_RING = 2000
RETRY_INTERVAL = 60.0
PRUNE_INTERVAL = 6 * 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT,
    profile TEXT,
    effective_mode TEXT,
    effective_profile TEXT,
    hottest_temp REAL,
    critical INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fan_samples (
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    fan_id TEXT NOT NULL,
    rpm INTEGER,
    duty INTEGER,
    requested_duty INTEGER
);
CREATE TABLE IF NOT EXISTS sensor_samples (
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    sensor_id TEXT NOT NULL,
    value REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_fan_samples ON fan_samples(sample_id, fan_id);
CREATE INDEX IF NOT EXISTS idx_sensor_samples ON sensor_samples(sample_id, sensor_id);
"""


class HistoryStore:
    """Bounded, degrading telemetry sink. Never raises into the control loop."""

    def __init__(self, path, retention_days=7, persist=True):
        self._lock = threading.RLock()
        self.path = pathlib.Path(path) if persist else None
        self.retention_days = max(1, int(retention_days))
        self.persist = bool(persist)
        self._conn = None
        self._buffer = collections.deque(maxlen=MEMORY_RING)
        self._pending = 0
        self._last_flush = time.monotonic()
        self._last_retry = 0.0
        self._last_prune = 0.0
        self.memory = collections.deque(maxlen=MEMORY_RING)
        self.degraded = False
        self.error = None
        self.writes = 0
        if self.persist:
            self._connect()

    def _connect(self):
        conn = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=0.05, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
            self.degraded = False
            self.error = None
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            self._degrade(f"cannot open {self.path}: {exc}")

    def _degrade(self, message):
        self.degraded = True
        self.error = message
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None

    @synchronized
    def append_sample(self, ts, mode, profile, effective_mode, effective_profile, hottest_temp, critical, fans, sensors):
        """Buffer one control tick. ``fans``: {fan_id: {rpm, duty, requested}}.
        ``sensors``: {sensor_id: value} (telemetry allowlist)."""
        try:
            stamp = float(ts)
        except (TypeError, ValueError, OverflowError):
            return
        if stamp != stamp or stamp in (float("inf"), float("-inf")):
            return
        sample = {
            "ts": stamp,
            "mode": mode,
            "profile": profile,
            "effective_mode": effective_mode,
            "effective_profile": effective_profile,
            "hottest_temp": hottest_temp,
            "critical": 1 if critical else 0,
            "fans": dict(fans or {}),
            "sensors": dict(sensors or {}),
        }
        self.memory.append(sample)
        if not self.persist:
            return
        self._buffer.append(sample)
        self._pending = len(self._buffer)
        now = time.monotonic()
        if self._pending >= FLUSH_EVERY or now - self._last_flush >= FLUSH_INTERVAL:
            self.flush()

    @synchronized
    def flush(self):
        """Write buffered samples; degrade instead of raising."""
        self._last_flush = time.monotonic()
        if not self._buffer:
            return
        if self._conn is None:
            self._maybe_recover()
            if self._conn is None:
                return
        try:
            conn = self._conn
            for sample in self._buffer:
                cursor = conn.execute(
                    "INSERT INTO samples(ts, mode, profile, effective_mode, effective_profile,"
                    " hottest_temp, critical) VALUES (?,?,?,?,?,?,?)",
                    (
                        sample["ts"], sample["mode"], sample["profile"],
                        sample["effective_mode"], sample["effective_profile"],
                        sample["hottest_temp"], sample["critical"],
                    ),
                )
                sample_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO fan_samples(sample_id, fan_id, rpm, duty, requested_duty)"
                    " VALUES (?,?,?,?,?)",
                    [
                        (sample_id, fan_id, row.get("rpm"), row.get("duty"), row.get("requested"))
                        for fan_id, row in sample["fans"].items()
                    ],
                )
                conn.executemany(
                    "INSERT INTO sensor_samples(sample_id, sensor_id, value) VALUES (?,?,?)",
                    [(sample_id, sensor_id, value) for sensor_id, value in sample["sensors"].items()],
                )
            conn.commit()
            self.writes += len(self._buffer)
            self._buffer.clear()
            self._pending = 0
            if self.degraded:
                self.degraded = False
                self.error = None
        except sqlite3.Error as exc:
            self._degrade(f"history write failed: {exc}")

    def _maybe_recover(self):
        now = time.monotonic()
        if now - self._last_retry < RETRY_INTERVAL:
            return
        self._last_retry = now
        if self.path is None:
            return
        probe = None
        try:
            probe = sqlite3.connect(str(self.path), timeout=0.05)
            probe.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            probe.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.Error, OSError) as exc:
            self.error = f"history unavailable: {exc}"
            return
        finally:
            if probe is not None:
                probe.close()
        if not os.access(str(self.path.parent), os.W_OK | os.X_OK):
            self.error = f"history unavailable: {self.path.parent} is not writable"
            return
        self._connect()

    @synchronized
    def mark_degraded(self, message):
        """External corruption report: quarantine the file and rebuild later."""
        self._degrade(message)
        if self.path is not None and self.path.exists():
            try:
                self.path.rename(self.path.with_name(
                    f"{self.path.stem}.corrupt-{int(time.time())}{self.path.suffix}"
                ))
            except OSError:
                pass

    @synchronized
    def prune(self, now=None):
        """Delete samples older than retention. Periodic, not just at startup."""
        if self._conn is None:
            return 0
        now = now or time.time()
        if now - self._last_prune < PRUNE_INTERVAL:
            return 0
        self._last_prune = now
        cutoff = now - self.retention_days * 86400
        try:
            cursor = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            self._degrade(f"history prune failed: {exc}")
            return 0

    def _memory_query(self, since, until, sensors, fans):
        rows = []
        for sample in self.memory:
            if since is not None and sample["ts"] < since:
                continue
            if until is not None and sample["ts"] > until:
                continue
            rows.append(sample)
        return self._downsample_memory(rows, sensors, fans)

    @staticmethod
    def _downsample_memory(rows, sensors, fans):
        points, fan_rows, sensor_rows = [], [], []
        for sample in rows:
            points.append({
                "ts": sample["ts"], "mode": sample["mode"], "profile": sample["profile"],
                "effective_mode": sample["effective_mode"],
                "effective_profile": sample["effective_profile"],
                "hottest_temp": sample["hottest_temp"], "critical": bool(sample["critical"]),
            })
            for fan_id, row in sample["fans"].items():
                if fans and fan_id not in fans:
                    continue
                fan_rows.append({"ts": sample["ts"], "fan_id": fan_id, **row})
            for sensor_id, value in sample["sensors"].items():
                if sensors and sensor_id not in sensors:
                    continue
                sensor_rows.append({"ts": sample["ts"], "sensor_id": sensor_id, "value": value})
        return points, fan_rows, sensor_rows

    @isolated_read
    def query(self, since=None, until=None, sensors=None, fans=None, max_points=None):
        """Presentation-oriented query; the daemon downsamples, not the UI."""
        if self._conn is None:
            points, fan_rows, sensor_rows = self._memory_query(since, until, sensors, fans)
            return self._limit_points(points, fan_rows, sensor_rows, max_points)
        try:
            args = []
            where = []
            if since is not None:
                where.append("s.ts >= ?")
                args.append(since)
            if until is not None:
                where.append("s.ts <= ?")
                args.append(until)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            samples = [
                dict(row)
                for row in self._conn.execute(
                    f"SELECT id, ts, mode, profile, effective_mode, effective_profile,"
                    f" hottest_temp, critical FROM samples s {clause} ORDER BY s.ts",
                    args,
                )
            ]
            fan_filter, fan_args = _in_filter("f.fan_id", fans)
            fan_rows = [
                {"ts": row[0], "fan_id": row[1], "rpm": row[2], "duty": row[3], "requested": row[4]}
                for row in self._conn.execute(
                    f"SELECT s.ts, f.fan_id, f.rpm, f.duty, f.requested_duty"
                    f" FROM fan_samples f JOIN samples s ON s.id = f.sample_id {clause}"
                    f"{' AND' if clause else 'WHERE'}{fan_filter}"
                    f" ORDER BY s.ts",
                    args + fan_args,
                )
            ]
            sensor_filter, sensor_args = _in_filter("n.sensor_id", sensors)
            sensor_rows = [
                {"ts": row[0], "sensor_id": row[1], "value": row[2]}
                for row in self._conn.execute(
                    f"SELECT s.ts, n.sensor_id, n.value"
                    f" FROM sensor_samples n JOIN samples s ON s.id = n.sample_id {clause}"
                    f"{' AND' if clause else 'WHERE'}{sensor_filter}"
                    f" ORDER BY s.ts",
                    args + sensor_args,
                )
            ]
        except sqlite3.DatabaseError as exc:
            self._degrade(f"history query failed: {exc}")
            points, fan_rows, sensor_rows = self._memory_query(since, until, sensors, fans)
            return self._limit_points(points, fan_rows, sensor_rows, max_points)
        return self._limit_points(samples, fan_rows, sensor_rows, max_points)

    def _limit_points(self, points, fan_rows, sensor_rows, max_points):
        if max_points and len(points) > int(max_points):
            points = _bucket_average(points, int(max_points))
            def _bucket(rows, identity, numeric):
                series = collections.defaultdict(list)
                for row in rows:
                    series[row[identity]].append(row)
                result = []
                for values in series.values():
                    result.extend(_bucket_average(values, int(max_points), numeric))
                return sorted(result, key=lambda row: (row["ts"], row[identity]))
            fan_rows = _bucket(fan_rows, "fan_id", ("rpm", "duty", "requested"))
            sensor_rows = _bucket(sensor_rows, "sensor_id", ("value",))
        for point in points:
            point["critical"] = bool(point.get("critical"))
        return {"samples": points, "fans": fan_rows, "sensors": sensor_rows, "degraded": self.degraded}

    @isolated_read
    def stats(self, since=None, until=None):
        """Aggregate statistics for the Analytics page."""
        if self._conn is None:
            return self._memory_stats(since, until)
        try:
            args, where = [], []
            if since is not None:
                where.append("ts >= ?")
                args.append(since)
            if until is not None:
                where.append("ts <= ?")
                args.append(until)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            row = self._conn.execute(
                f"SELECT COUNT(*), AVG(hottest_temp), MAX(hottest_temp), SUM(critical)"
                f" FROM samples {clause}",
                args,
            ).fetchone()
            profiles = [
                {"profile": r[0] or "unknown", "samples": r[1]}
                for r in self._conn.execute(
                    f"SELECT effective_profile, COUNT(*) FROM samples {clause}"
                    f" GROUP BY effective_profile ORDER BY COUNT(*) DESC",
                    args,
                )
            ]
            bands = [
                {"fan_id": r[0], "band": r[1], "samples": r[2]}
                for r in self._conn.execute(
                    f"SELECT f.fan_id,"
                    f" CASE WHEN f.duty < 20 THEN '0-20' WHEN f.duty < 40 THEN '20-40'"
                    f" WHEN f.duty < 60 THEN '40-60' WHEN f.duty < 80 THEN '60-80' ELSE '80-100' END,"
                    f" COUNT(*) FROM fan_samples f JOIN samples s ON s.id = f.sample_id {clause}"
                    f" GROUP BY f.fan_id, 2 ORDER BY f.fan_id",
                    args,
                )
            ]
            per_fan = [
                {"fan_id": r[0], "avg_duty": r[1], "max_duty": r[2], "avg_rpm": r[3], "max_rpm": r[4]}
                for r in self._conn.execute(
                    f"SELECT f.fan_id, AVG(f.duty), MAX(f.duty), AVG(f.rpm), MAX(f.rpm)"
                    f" FROM fan_samples f JOIN samples s ON s.id = f.sample_id {clause}"
                    f" GROUP BY f.fan_id ORDER BY f.fan_id",
                    args,
                )
            ]
        except sqlite3.DatabaseError as exc:
            self._degrade(f"history stats failed: {exc}")
            return self._memory_stats(since, until)
        return {
            "samples": row[0] or 0,
            "avg_temp": row[1],
            "max_temp": row[2],
            "critical_events": row[3] or 0,
            "profiles": profiles,
            "duty_bands": bands,
            "per_fan": per_fan,
            "degraded": self.degraded,
        }

    def _memory_stats(self, since, until):
        rows = [
            s for s in self.memory
            if (since is None or s["ts"] >= since) and (until is None or s["ts"] <= until)
        ]
        temps = [s["hottest_temp"] for s in rows if s["hottest_temp"] is not None]
        profile_counts = collections.Counter(
            s["effective_profile"] or "unknown" for s in rows
        )
        band_counts = collections.Counter()
        per_fan = collections.defaultdict(list)
        for s in rows:
            for fan_id, row in s["fans"].items():
                duty = row.get("duty")
                if duty is None:
                    continue
                band = min(int(duty // 20), 4) * 20
                band_counts[(fan_id, f"{band}-{band + 20}")] += 1
                per_fan[fan_id].append((row.get("duty"), row.get("rpm")))
        return {
            "samples": len(rows),
            "avg_temp": (sum(temps) / len(temps)) if temps else None,
            "max_temp": max(temps) if temps else None,
            "critical_events": sum(1 for s in rows if s["critical"]),
            "profiles": [
                {"profile": name, "samples": count}
                for name, count in profile_counts.most_common()
            ],
            "duty_bands": [
                {"fan_id": fan_id, "band": band, "samples": count}
                for (fan_id, band), count in sorted(band_counts.items())
            ],
            "per_fan": [
                {
                    "fan_id": fan_id,
                    "avg_duty": sum(d for d, _ in values) / len(values),
                    "max_duty": max(d for d, _ in values),
                    "avg_rpm": _avg_non_null(r for _, r in values),
                    "max_rpm": _max_non_null(r for _, r in values),
                }
                for fan_id, values in sorted(per_fan.items())
            ],
            "degraded": self.degraded,
        }

    @synchronized
    def status(self):
        return {
            "persist": self.persist,
            "path": str(self.path) if self.path else None,
            "degraded": self.degraded,
            "error": self.error,
            "memory_samples": len(self.memory),
            "buffered": len(self._buffer),
            "writes": self.writes,
            "retention_days": self.retention_days,
        }

    @isolated_read
    def export_csv(self, since=None, until=None):
        rows = self.query(since=since, until=until)
        headers = ["time", "mode", "effective_profile", "hottest_temp", "critical",
                   "fan_id", "duty", "requested", "rpm", "sensor_id", "sensor_value"]
        lines = [",".join(headers)]
        by_ts_fan = {}
        for row in rows["fans"]:
            by_ts_fan.setdefault((row["ts"], row["fan_id"]), row)
        by_ts_sensor = {}
        for row in rows["sensors"]:
            by_ts_sensor.setdefault((row["ts"], row["sensor_id"]), row)
        fan_ids = sorted({row["fan_id"] for row in rows["fans"]})
        sensor_ids = sorted({row["sensor_id"] for row in rows["sensors"]})
        for point in rows["samples"]:
            ts = point["ts"]
            for fan_id in fan_ids:
                row = by_ts_fan.get((ts, fan_id)) or {}
                lines.append(",".join(_csv_cells(
                    _iso(ts), point.get("mode"), point.get("effective_profile"),
                    point.get("hottest_temp"), int(bool(point.get("critical"))),
                    fan_id, row.get("duty"), row.get("requested"), row.get("rpm"),
                    "", "",
                )))
            for sensor_id in sensor_ids:
                row = by_ts_sensor.get((ts, sensor_id)) or {}
                lines.append(",".join(_csv_cells(
                    _iso(ts), point.get("mode"), point.get("effective_profile"),
                    point.get("hottest_temp"), int(bool(point.get("critical"))),
                    "", "", "", "", sensor_id, row.get("value"),
                )))
        return "\n".join(lines) + "\n"

    @synchronized
    def close(self):
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - close must never raise into callers
            pass
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None


def _avg_non_null(values):
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _max_non_null(values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _in_filter(column, values):
    """SQL fragment + args restricting ``column`` to ``values`` (or no-op)."""
    if not values:
        return " 1=1", []
    listed = list(values)
    placeholders = ", ".join("?" for _ in listed)
    return f" {column} IN ({placeholders})", listed


def _csv_cells(*values):
    return ["" if v is None else str(v) for v in values]


def _bucket_average(points, max_points, numeric=("hottest_temp",)):
    """Time-bucket average downsampling preserving start and end points."""
    if len(points) <= max_points:
        return points
    span = points[-1]["ts"] - points[0]["ts"]
    buckets = {}
    for point in points:
        offset = (point["ts"] - points[0]["ts"]) / span * max_points if span else 0
        buckets.setdefault(min(int(offset), max_points - 1), []).append(point)
    result = []
    for index in sorted(buckets):
        rows = buckets[index]
        merged = dict(rows[len(rows) // 2])
        for key in numeric:
            values = [r[key] for r in rows if r.get(key) is not None]
            merged[key] = sum(values) / len(values) if values else None
        merged["ts"] = rows[0]["ts"]
        if "critical" in merged:
            merged["critical"] = any(r.get("critical") for r in rows)
        result.append(merged)
    result[0]["ts"] = points[0]["ts"]
    result[-1]["ts"] = points[-1]["ts"]
    return result
