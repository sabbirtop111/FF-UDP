"""Process and host health for the safety panel.

Two ideas live here:

* the **hosting box** must stay comfortable — so the dashboard reports its own
  footprint (memory, load, database size, event-loop responsiveness) instead of
  the operator having to guess from a hosting dashboard;
* the **game API** must never be hammered — those numbers come from
  ``BotManager.safety_payload()`` which only reads in-memory counters.

Nothing in this module performs network I/O.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

_PROCESS_STARTED_AT = time.time()


def _read_meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    values["rss_kb"] = int(line.split()[1])
    except Exception:
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    values["host_total_kb"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    values["host_available_kb"] = int(line.split()[1])
    except Exception:
        pass
    return values


def _load_average() -> Optional[list[float]]:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except Exception:
        return None


def _directory_size_bytes(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except Exception:
                continue
    except Exception:
        return 0
    return total


def collect_host_health(*, database_path: str, loop_lag_ms: Optional[float] = None,
                        extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A small, honest snapshot of what this process is costing the host."""
    mem = _read_meminfo()
    cpu_count = os.cpu_count() or 1
    load = _load_average()
    db_path = Path(database_path)
    db_dir = db_path.parent if db_path.parent.exists() else Path(".")

    disk: Dict[str, Any] = {}
    try:
        usage = shutil.disk_usage(str(db_dir))
        disk = {
            "free_mb": round(usage.free / (1024 * 1024), 1),
            "used_percent": round(usage.used * 100.0 / usage.total, 1) if usage.total else 0.0,
        }
    except Exception:
        disk = {}

    rss_mb = round(mem.get("rss_kb", 0) / 1024, 1) if mem.get("rss_kb") else None
    host_total_mb = round(mem.get("host_total_kb", 0) / 1024, 1) if mem.get("host_total_kb") else None
    host_available_mb = round(mem.get("host_available_kb", 0) / 1024, 1) if mem.get("host_available_kb") else None

    # A single number an operator can act on: our own share of the box.
    load_per_core = round(load[0] / cpu_count, 2) if load else None
    pressure = "low"
    if load_per_core is not None:
        if load_per_core >= 1.0:
            pressure = "high"
        elif load_per_core >= 0.6:
            pressure = "moderate"
    if rss_mb is not None and host_total_mb and rss_mb / host_total_mb > 0.6:
        pressure = "high"
    if loop_lag_ms is not None and loop_lag_ms > 250:
        pressure = "high" if pressure != "high" else pressure

    payload: Dict[str, Any] = {
        "process_uptime_seconds": int(max(0, time.time() - _PROCESS_STARTED_AT)),
        "memory_mb": rss_mb,
        "host_memory_total_mb": host_total_mb,
        "host_memory_available_mb": host_available_mb,
        "cpu_count": cpu_count,
        "load_average": load,
        "load_per_core": load_per_core,
        "pressure": pressure,
        "loop_lag_ms": round(loop_lag_ms, 1) if loop_lag_ms is not None else None,
        "database_mb": round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0.0,
        "data_dir_mb": round(_directory_size_bytes(db_dir) / (1024 * 1024), 2),
        "disk_free_mb": disk.get("free_mb"),
        "disk_used_percent": disk.get("used_percent"),
    }
    if extra:
        payload.update(extra)
    return payload


def pressure_note(payload: Dict[str, Any]) -> str:
    """A plain-language line for the panel (no developer wording)."""
    pressure = payload.get("pressure")
    if pressure == "high":
        return "This instance is working hard — consider a bigger plan or fewer accounts at once."
    if pressure == "moderate":
        return "Comfortable, with headroom: nothing on this instance is overloaded."
    return "Comfortable: memory, CPU and disk all have plenty of room."
