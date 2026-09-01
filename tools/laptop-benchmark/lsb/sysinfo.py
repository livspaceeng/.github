"""Hardware and OS inventory.

macOS/Apple Silicon is the first-class target (P/E core split, GPU core count,
battery health, thermal-relevant chassis info). Linux is supported so the same
tool can run on desktops and in CI.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import platform
import re
import shutil

from .util import IS_LINUX, IS_MAC, out, run, sysctl


def _macos_gpu():
    """GPU name and core count from system_profiler (slow, so called once)."""
    info = {"name": None, "cores": None, "metal": None}
    rc, so, _ = run(["system_profiler", "-json", "SPDisplaysDataType"], timeout=45)
    if rc != 0:
        return info
    try:
        items = json.loads(so).get("SPDisplaysDataType", [])
    except json.JSONDecodeError:
        return info
    if not items:
        return info
    g = items[0]
    info["name"] = g.get("sppci_model") or g.get("_name")
    cores = g.get("sppci_cores") or g.get("spdisplays_gpu_cores")
    if cores:
        m = re.search(r"\d+", str(cores))
        info["cores"] = int(m.group()) if m else None
    info["metal"] = g.get("spdisplays_mtlgpufamilysupport")
    return info


def _macos_battery():
    info = {"present": False}
    rc, so, _ = run(["system_profiler", "-json", "SPPowerDataType"], timeout=45)
    if rc == 0:
        try:
            data = json.loads(so).get("SPPowerDataType", [])
        except json.JSONDecodeError:
            data = []
        for entry in data:
            health = entry.get("sppower_battery_health_info")
            if health:
                info["present"] = True
                info["cycle_count"] = health.get("sppower_battery_cycle_count")
                info["condition"] = health.get("sppower_battery_health")
                info["max_capacity_pct"] = health.get("sppower_battery_health_maximum_capacity")
    batt = out(["pmset", "-g", "batt"])
    if batt:
        info["power_source"] = "AC" if "AC Power" in batt else "Battery"
        m = re.search(r"(\d+)%", batt)
        if m:
            info["charge_pct"] = int(m.group(1))
    return info


def _macos_disk(path):
    info = {}
    rc, so, _ = run(["diskutil", "info", "-plist", "/"], timeout=20)
    if rc == 0:
        m = re.search(r"<key>SolidState</key>\s*<(true|false)/>", so)
        if m:
            info["ssd"] = m.group(1) == "true"
        m = re.search(r"<key>MediaName</key>\s*<string>([^<]*)</string>", so)
        if m:
            info["media"] = m.group(1)
    return info


def collect(scratch_path=None):
    """Return a dict describing the machine under test."""
    scratch_path = scratch_path or os.getcwd()
    ncpu = multiprocessing.cpu_count()
    info = {
        "platform": platform.system(),
        "os_version": platform.mac_ver()[0] if IS_MAC else platform.release(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "python": platform.python_version(),
        "logical_cpus": ncpu,
        "perf_cores": None,
        "eff_cores": None,
        "cpu": platform.processor() or platform.machine(),
        "ram_bytes": None,
        "gpu": {},
        "battery": {},
        "disk": {},
    }

    if IS_MAC:
        info["cpu"] = sysctl("machdep.cpu.brand_string", info["cpu"])
        info["model"] = sysctl("hw.model")
        info["ram_bytes"] = sysctl("hw.memsize", cast=int)
        info["perf_cores"] = sysctl("hw.perflevel0.physicalcpu", cast=int)
        info["eff_cores"] = sysctl("hw.perflevel1.physicalcpu", cast=int)
        info["physical_cpus"] = sysctl("hw.physicalcpu", cast=int)
        info["apple_silicon"] = bool(sysctl("hw.optional.arm64", 0, int))
        info["gpu"] = _macos_gpu()
        info["battery"] = _macos_battery()
        info["disk"] = _macos_disk(scratch_path)
        info["kernel"] = out(["uname", "-v"])[:120]
    elif IS_LINUX:
        try:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        info["cpu"] = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        info["ram_bytes"] = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass
        info["physical_cpus"] = ncpu
        info["apple_silicon"] = False
        renderer = out(["glxinfo", "-B"]) if shutil.which("glxinfo") else ""
        m = re.search(r"Device:\s*(.+)", renderer)
        info["gpu"] = {"name": m.group(1).strip() if m else None, "cores": None}

    try:
        usage = shutil.disk_usage(scratch_path)
        info["disk"].update({
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "scratch_path": scratch_path,
        })
    except OSError:
        pass

    # Cores we will treat as "the fast ones" for CAD-style bursts.
    info["burst_threads"] = info.get("perf_cores") or max(1, ncpu // 2 or 1)
    return info


def summary_lines(info):
    ram = info.get("ram_bytes")
    ram_gb = f"{ram / (1024**3):.0f} GB" if ram else "unknown"
    cores = f"{info['logical_cpus']} logical"
    if info.get("perf_cores") and info.get("eff_cores"):
        cores = f"{info['perf_cores']}P + {info['eff_cores']}E ({info['logical_cpus']} logical)"
    gpu = info.get("gpu") or {}
    gpu_s = gpu.get("name") or "unknown"
    if gpu.get("cores"):
        gpu_s += f" ({gpu['cores']} cores)"
    disk = info.get("disk") or {}
    lines = [
        f"Machine     : {info.get('model') or info.get('hostname')}",
        f"CPU         : {info.get('cpu')}",
        f"Cores       : {cores}",
        f"Memory      : {ram_gb}",
        f"GPU         : {gpu_s}",
        f"OS          : {info.get('platform')} {info.get('os_version')} ({info.get('arch')})",
    ]
    if disk.get("free_bytes"):
        lines.append(f"Disk free   : {disk['free_bytes'] / (1024**3):.0f} GB on {disk.get('scratch_path')}")
    batt = info.get("battery") or {}
    if batt.get("present"):
        lines.append(
            f"Battery     : {batt.get('condition', '?')}, {batt.get('cycle_count', '?')} cycles, "
            f"on {batt.get('power_source', '?')} at {batt.get('charge_pct', '?')}%"
        )
    return lines
