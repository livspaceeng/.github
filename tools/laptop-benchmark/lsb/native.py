"""Build and invoke the native kernel binary (lsburn)."""
from __future__ import annotations

import json
import os
import subprocess

from .util import IS_MAC, run, which

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "kernels.c")


class NativeUnavailable(RuntimeError):
    pass


def build(outdir):
    """Compile kernels.c into `outdir`/lsburn. Returns the binary path."""
    os.makedirs(outdir, exist_ok=True)
    binary = os.path.join(outdir, "lsburn")
    if os.path.exists(binary) and os.path.getmtime(binary) > os.path.getmtime(SOURCE):
        return binary

    cc = which("cc", "clang", "gcc")
    if not cc:
        raise NativeUnavailable(
            "no C compiler found. On macOS run:  xcode-select --install"
        )

    base = [cc, "-O3", "-funroll-loops", "-pthread", "-o", binary, SOURCE, "-lm"]
    # -mcpu=apple-m1 lets clang use the full Apple Silicon ISA; harmless to try.
    variants = [base[:1] + (["-mcpu=apple-m1"] if IS_MAC else ["-march=native"]) + base[1:], base]
    last = ""
    for cmd in variants:
        rc, _, err = run(cmd, timeout=120)
        if rc == 0:
            return binary
        last = err
    raise NativeUnavailable(f"compilation failed: {last.strip()[:400]}")


def call(binary, mode, **kwargs):
    """Run one kernel and return its parsed JSON result."""
    cmd = [binary, mode]
    for key, value in kwargs.items():
        if value is None:
            continue
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"mode": mode, "error": "timed out"}
    if p.returncode != 0:
        return {"mode": mode, "error": p.stderr.strip()[:200] or f"rc={p.returncode}"}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"mode": mode, "error": f"unparseable output: {p.stdout[:200]!r}"}
