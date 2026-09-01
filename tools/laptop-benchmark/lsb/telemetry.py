"""Background telemetry sampling during benchmark and stress phases.

macOS backend (primary)
  * CPU utilisation + physical memory : streamed from a single long-lived `top`
  * Swap                              : sysctl vm.swapusage
  * Page-in/out and compressor        : vm_stat
  * Thermal throttling                : pmset -g therm  (CPU_Speed_Limit < 100)
  * GPU utilisation                   : ioreg AGXAccelerator PerformanceStatistics
  * Power + thermal pressure          : powermetrics, only when sudo is available

None of the default path needs root. powermetrics is strictly an enrichment.

Linux backend (secondary, used for desktops and for this tool's own CI self-test)
  * /proc/stat, /proc/meminfo, /proc/pressure, /sys/class/thermal
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from .util import IS_LINUX, IS_MAC, out, run

PAGE_RE = re.compile(r"page size of (\d+) bytes")
TOP_CPU_RE = re.compile(
    r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle"
)
TOP_MEM_RE = re.compile(
    r"PhysMem:\s*([\d.]+)([BKMGT])\s+used\s*\(([\d.]+)([BKMGT])\s+wired.*?\),\s*([\d.]+)([BKMGT])\s+unused"
)
SWAP_RE = re.compile(r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M")
GPU_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
THERM_RE = re.compile(r"CPU_Speed_Limit\s*=\s*(\d+)")

_UNIT = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _scaled(value, unit):
    return float(value) * _UNIT.get(unit.upper(), 1)


class Sampler:
    """Collects a timestamped metric sample every `interval` seconds."""

    def __init__(self, interval=2.0):
        self.interval = interval
        self.samples = []
        self.phase = "idle"
        self._stop = threading.Event()
        self._threads = []
        self._procs = []
        self._lock = threading.Lock()
        self._live = {}          # values pushed by streaming readers
        self._t0 = None
        self._prev_cpu = None    # Linux /proc/stat delta
        self.notes = []

    # -- lifecycle ---------------------------------------------------

    def start(self):
        self._t0 = time.monotonic()
        if IS_MAC:
            self._start_top()
            self._start_powermetrics()
        t = threading.Thread(target=self._loop, name="lsb-sampler", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        for t in self._threads:
            t.join(timeout=5)
        return self.samples

    def set_phase(self, name):
        with self._lock:
            self.phase = name

    def mark(self, note):
        self.notes.append({"t": self._elapsed(), "note": note})

    def _elapsed(self):
        return round(time.monotonic() - (self._t0 or time.monotonic()), 3)

    # -- streaming readers (macOS) -----------------------------------

    def _spawn_reader(self, cmd, handler, name):
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except (OSError, ValueError) as exc:
            self.notes.append({"t": 0, "note": f"{name} unavailable: {exc}"})
            return None
        self._procs.append(p)

        def pump():
            try:
                for line in p.stdout:
                    if self._stop.is_set():
                        break
                    handler(line)
            except Exception:
                pass

        t = threading.Thread(target=pump, name=f"lsb-{name}", daemon=True)
        t.start()
        self._threads.append(t)
        return p

    def _start_top(self):
        def handle(line):
            m = TOP_CPU_RE.search(line)
            if m:
                user, sysm, idle = (float(g) for g in m.groups())
                with self._lock:
                    self._live.update(
                        cpu_user=user, cpu_sys=sysm, cpu_idle=idle,
                        cpu_busy=round(100.0 - idle, 2),
                    )
                return
            m = TOP_MEM_RE.search(line)
            if m:
                used, uu, wired, wu, unused, nu = m.groups()
                with self._lock:
                    self._live.update(
                        mem_used_bytes=_scaled(used, uu),
                        mem_wired_bytes=_scaled(wired, wu),
                        mem_unused_bytes=_scaled(unused, nu),
                    )

        interval = max(1, int(self.interval))
        self._spawn_reader(
            ["top", "-l", "0", "-s", str(interval), "-n", "0"], handle, "top"
        )

    def _start_powermetrics(self):
        """Optional: only if sudo works without a password prompt."""
        rc, _, _ = run(["sudo", "-n", "true"], timeout=5)
        if rc != 0:
            self.notes.append({
                "t": 0,
                "note": "powermetrics skipped (needs passwordless sudo); "
                        "package power and per-cluster frequency not recorded",
            })
            return

        def handle(line):
            for key, pat in (
                ("cpu_power_w", r"CPU Power:\s*([\d.]+)\s*mW"),
                ("gpu_power_w", r"GPU Power:\s*([\d.]+)\s*mW"),
                ("package_power_w", r"Combined Power \(CPU \+ GPU \+ ANE\):\s*([\d.]+)\s*mW"),
            ):
                m = re.search(pat, line)
                if m:
                    with self._lock:
                        self._live[key] = float(m.group(1)) / 1000.0
                    return
            m = re.search(r"E-Cluster HW active frequency:\s*(\d+)\s*MHz", line)
            if m:
                with self._lock:
                    self._live["e_cluster_mhz"] = int(m.group(1))
                return
            m = re.search(r"P-Cluster HW active frequency:\s*(\d+)\s*MHz", line)
            if m:
                with self._lock:
                    self._live["p_cluster_mhz"] = int(m.group(1))
                return
            m = re.search(r"pressure level:\s*(\w+)", line)
            if m:
                with self._lock:
                    self._live["thermal_pressure"] = m.group(1)

        interval_ms = max(1000, int(self.interval * 1000))
        self._spawn_reader(
            ["sudo", "-n", "powermetrics", "--samplers", "cpu_power,gpu_power,thermal",
             "-i", str(interval_ms)],
            handle, "powermetrics",
        )

    # -- polled collectors -------------------------------------------

    def _poll_mac(self):
        s = {}
        swap = out(["sysctl", "-n", "vm.swapusage"], timeout=5)
        m = SWAP_RE.search(swap)
        if m:
            total, used, free = (float(g) * 1024**2 for g in m.groups())
            s.update(swap_total_bytes=total, swap_used_bytes=used, swap_free_bytes=free)

        vm = out(["vm_stat"], timeout=5)
        if vm:
            pm = PAGE_RE.search(vm)
            page = int(pm.group(1)) if pm else 4096
            fields = {}
            for line in vm.splitlines()[1:]:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                v = v.strip().rstrip(".")
                if v.isdigit():
                    fields[k.strip()] = int(v)
            s["mem_compressed_bytes"] = fields.get("Pages occupied by compressor", 0) * page
            s["pageins"] = fields.get("Pageins", 0)
            s["pageouts"] = fields.get("Pageouts", 0)
            s["swapins"] = fields.get("Swapins", 0)
            s["swapouts"] = fields.get("Swapouts", 0)

        therm = out(["pmset", "-g", "therm"], timeout=5)
        m = THERM_RE.search(therm)
        s["cpu_speed_limit"] = int(m.group(1)) if m else None

        gpu = out(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "AGXAccelerator"], timeout=8)
        if not gpu:
            gpu = out(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=8)
        m = GPU_RE.search(gpu or "")
        s["gpu_util_pct"] = float(m.group(1)) if m else None
        return s

    def _poll_linux(self):
        s = {}
        try:
            with open("/proc/stat") as fh:
                parts = fh.readline().split()
            vals = [int(x) for x in parts[1:8]]
            idle = vals[3] + vals[4]
            total = sum(vals)
            if self._prev_cpu:
                d_total = total - self._prev_cpu[0]
                d_idle = idle - self._prev_cpu[1]
                if d_total > 0:
                    busy = 100.0 * (d_total - d_idle) / d_total
                    s["cpu_busy"] = round(busy, 2)
                    s["cpu_idle"] = round(100.0 - busy, 2)
            self._prev_cpu = (total, idle)
        except OSError:
            pass

        mem = {}
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, _, rest = line.partition(":")
                    mem[k] = int(rest.split()[0]) * 1024
        except OSError:
            pass
        if mem:
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            s.update(
                mem_used_bytes=total - avail,
                mem_unused_bytes=avail,
                swap_total_bytes=mem.get("SwapTotal", 0),
                swap_used_bytes=mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            )

        temps = []
        try:
            import glob
            for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
                try:
                    with open(zone) as fh:
                        temps.append(int(fh.read().strip()) / 1000.0)
                except (OSError, ValueError):
                    pass
        except Exception:
            pass
        if temps:
            s["temp_c"] = round(max(temps), 1)

        # No hardware speed-limit signal on generic Linux; report as unknown
        # rather than pretending the machine never throttled.
        s["cpu_speed_limit"] = None
        return s

    # -- main loop ---------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                sample = self._poll_mac() if IS_MAC else self._poll_linux() if IS_LINUX else {}
            except Exception as exc:
                sample = {"error": str(exc)}
            with self._lock:
                sample.update(self._live)
                sample["phase"] = self.phase
            sample["t"] = self._elapsed()
            try:
                import os
                sample["load1"] = round(os.getloadavg()[0], 2)
            except (OSError, AttributeError):
                pass
            self.samples.append(sample)
            self._stop.wait(max(0.1, self.interval - (time.monotonic() - started)))


def phase_slice(samples, phase):
    return [s for s in samples if s.get("phase") == phase]


def series(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]
