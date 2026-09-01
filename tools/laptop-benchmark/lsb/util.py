"""Small shared helpers: subprocess wrappers, formatting, platform detection."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys


IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def run(cmd, timeout=20, check=False):
    """Run a command, return (rc, stdout, stderr). Never raises on non-zero."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=isinstance(cmd, str),
        )
        if check and p.returncode != 0:
            raise RuntimeError(f"{cmd} failed rc={p.returncode}: {p.stderr.strip()}")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def out(cmd, default="", timeout=20):
    """Run a command and return its trimmed stdout, or `default` on any failure."""
    rc, so, _ = run(cmd, timeout=timeout)
    return so.strip() if rc == 0 else default


def sysctl(key, default=None, cast=str):
    value = out(["sysctl", "-n", key])
    if not value:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def human_bytes(n):
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PiB"


def pct(part, whole):
    if not whole:
        return 0.0
    return 100.0 * float(part) / float(whole)


def percentile(values, q):
    """Linear-interpolated percentile. `q` in 0..100."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def platform_label():
    if IS_MAC:
        return f"macOS {platform.mac_ver()[0] or platform.release()} ({platform.machine()})"
    return f"{platform.system()} {platform.release()} ({platform.machine()})"
