"""Capability microbenchmarks: CPU, memory bandwidth, storage.

Run once before the stress phase (baseline = what the machine can do when
nothing else is competing) and again immediately after (sustained = what it
can still do once it is hot and full). The gap between the two is the single
most useful number this tool produces for a laptop.
"""
from __future__ import annotations

import os
import random
import time

from . import native
from .util import IS_MAC, percentile

# macOS fcntl command to bypass the unified buffer cache, so a read benchmark
# measures the SSD instead of RAM.
F_NOCACHE = 48


def cpu_suite(binary, threads, quick=False, seconds=None):
    """Single-thread, all-thread, and (unless quick) a scaling curve."""
    dur = seconds if seconds else (3.0 if quick else 6.0)
    results = {
        "single_thread": native.call(binary, "matmul", n=512, threads=1, seconds=dur),
        "all_threads": native.call(binary, "matmul", n=512, threads=threads, seconds=dur),
        "geom_single": native.call(binary, "geom", verts=1500000, threads=1, seconds=dur / 2),
        "geom_all": native.call(binary, "geom", verts=1500000, threads=threads, seconds=dur / 2),
    }
    single = results["single_thread"].get("gflops")
    multi = results["all_threads"].get("gflops")
    if single and multi:
        results["multicore_scaling"] = round(multi / single, 2)

    if not quick:
        curve = []
        n = 1
        while n <= threads:
            r = native.call(binary, "matmul", n=512, threads=n, seconds=2.5)
            curve.append({"threads": n, "gflops": r.get("gflops")})
            n *= 2
        if curve and curve[-1]["threads"] != threads:
            r = native.call(binary, "matmul", n=512, threads=threads, seconds=2.5)
            curve.append({"threads": threads, "gflops": r.get("gflops")})
        results["scaling_curve"] = curve
    return results


def memory_suite(binary, threads, quick=False):
    dur = 3.0 if quick else 5.0
    return {
        "triad_single": native.call(binary, "stream", mib=512, threads=1, seconds=dur),
        "triad_all": native.call(binary, "stream", mib=512, threads=threads, seconds=dur),
    }


def _open_uncached(path, flags):
    fd = os.open(path, flags)
    if IS_MAC:
        try:
            import fcntl
            fcntl.fcntl(fd, F_NOCACHE, 1)
        except OSError:
            pass
    return fd


def disk_suite(scratch, size_mb=1024, quick=False):
    """Sequential write (fsync'd), uncached sequential read, 4K random read IOPS."""
    os.makedirs(scratch, exist_ok=True)
    import shutil
    free = shutil.disk_usage(scratch).free
    size_mb = min(size_mb, max(64, int(free / (1 << 20) * 0.25)))
    if quick:
        size_mb = min(size_mb, 256)
    path = os.path.join(scratch, "diskbench.bin")
    block = os.urandom(1 << 20)
    out = {"size_mb": size_mb, "path": scratch, "cache_bypassed": IS_MAC}

    try:
        t0 = time.monotonic()
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            for _ in range(size_mb):
                os.write(fd, block)
            os.fsync(fd)
        finally:
            os.close(fd)
        out["seq_write_mb_s"] = round(size_mb / (time.monotonic() - t0), 1)

        fd = _open_uncached(path, os.O_RDONLY)
        try:
            t0 = time.monotonic()
            read = 0
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                read += len(b)
            out["seq_read_mb_s"] = round(read / (1 << 20) / (time.monotonic() - t0), 1)

            # 4 KiB random reads: the access pattern of opening a linked assembly.
            rng = random.Random(7)
            span = max(1, (size_mb << 20) - 8192)
            n_ops = 2000 if quick else 6000
            lat = []
            t0 = time.monotonic()
            for _ in range(n_ops):
                off = rng.randrange(0, span) & ~4095
                s = time.monotonic()
                os.pread(fd, 4096, off)
                lat.append((time.monotonic() - s) * 1000.0)
            total = time.monotonic() - t0
            out["rand_read_iops"] = round(n_ops / total)
            out["rand_read_lat_ms_p50"] = round(percentile(lat, 50), 3)
            out["rand_read_lat_ms_p99"] = round(percentile(lat, 99), 3)
        finally:
            os.close(fd)
    except OSError as exc:
        out["error"] = str(exc)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return out


def sustained_check(binary, threads, seconds=6.0):
    """Re-measure peak CPU right at the end of the stress phase."""
    return {
        "single_thread": native.call(binary, "matmul", n=512, threads=1, seconds=seconds),
        "all_threads": native.call(binary, "matmul", n=512, threads=threads, seconds=seconds),
    }
