"""Child process that emulates a CAD application holding a large assembly open.

Run as:  python3 -m lsb.cadproc --events FILE --burn PATH [...]

It reproduces the four behaviours that actually make CAD hard on a laptop:

  1. A large, incompressible resident working set (the model in memory).
  2. A continuously busy single thread - geometry kernels such as Parasolid and
     ACIS are overwhelmingly single-threaded, which is why CAD rewards
     single-core speed far more than core count.
  3. Periodic all-P-core bursts - rebuild, tessellation, boolean ops.
  4. Periodic large fsync'd writes - autosave / pack-and-go.

It lives in its own process so its RSS shows up separately in Activity Monitor
and in this tool's telemetry.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time

CHUNK_MB = 64


def log(fh, **event):
    event["t"] = round(time.monotonic(), 3)
    fh.write(json.dumps(event) + "\n")
    fh.flush()


def allocate_working_set(target_mb, fh):
    """Allocate `target_mb` MiB of page-level-incompressible resident memory."""
    chunks = []
    seed = os.urandom(1 << 20)              # 1 MiB of true random
    per_chunk = CHUNK_MB * (1 << 20)
    tile = seed * CHUNK_MB                  # each 16K page is distinct random data
    n = max(1, target_mb // CHUNK_MB)
    t0 = time.monotonic()
    for i in range(n):
        buf = bytearray(tile)
        # Salt so the chunks are not byte-identical, in case of page dedup.
        for off in range(0, per_chunk, 1 << 16):
            buf[off] = (i * 37 + off) & 0xFF
        chunks.append(buf)
        # Faulting in several GiB takes real time on a memory-constrained
        # machine; report progress so the run never looks hung.
        if (i + 1) % 16 == 0 or i + 1 == n:
            done_mib = (i + 1) * CHUNK_MB
            elapsed = time.monotonic() - t0
            log(fh, event="working_set_progress", mib=done_mib, of_mib=n * CHUNK_MB,
                mib_per_s=round(done_mib / max(elapsed, 1e-6), 1))
    elapsed = time.monotonic() - t0
    log(fh, event="working_set_ready", mib=n * CHUNK_MB,
        alloc_seconds=round(elapsed, 2),
        alloc_mib_per_s=round(n * CHUNK_MB / max(elapsed, 1e-6), 1))
    return chunks


def touch(chunks, rng, ops):
    """Random-access traversal of the model, as a viewport pan/zoom would do."""
    if not chunks:
        return
    size = len(chunks[0])
    for _ in range(ops):
        c = chunks[rng.randrange(len(chunks))]
        off = rng.randrange(0, size - 8)
        c[off] = (c[off] + 1) & 0xFF


def autosave(scratch, mb, rng, fh, index):
    path = os.path.join(scratch, f"autosave_{index % 3}.cad")
    block = bytes(rng.getrandbits(8) for _ in range(1 << 16)) * 16   # 1 MiB
    t0 = time.monotonic()
    written = 0
    try:
        with open(path, "wb") as f:
            for _ in range(mb):
                f.write(block)
                written += len(block)
            f.flush()
            os.fsync(f.fileno())
        write_s = time.monotonic() - t0
        # Read a reference part back in, as a linked-assembly load would.
        t1 = time.monotonic()
        with open(path, "rb") as f:
            read = 0
            while read < written // 2:
                b = f.read(1 << 20)
                if not b:
                    break
                read += len(b)
        log(fh, event="autosave", mib=written / (1 << 20),
            write_mb_s=round(written / (1 << 20) / max(write_s, 1e-6), 1),
            read_mb_s=round(read / (1 << 20) / max(time.monotonic() - t1, 1e-6), 1))
    except OSError as exc:
        log(fh, event="autosave_failed", error=str(exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description="CAD application emulator")
    ap.add_argument("--events", required=True)
    ap.add_argument("--burn", required=True, help="path to the lsburn binary")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--working-set-mb", type=int, default=4096)
    ap.add_argument("--burst-every", type=float, default=20.0)
    ap.add_argument("--burst-seconds", type=float, default=4.0)
    ap.add_argument("--burst-threads", type=int, default=4)
    ap.add_argument("--autosave-every", type=float, default=60.0)
    ap.add_argument("--autosave-mb", type=int, default=200)
    args = ap.parse_args(argv)

    os.makedirs(args.scratch, exist_ok=True)
    rng = random.Random(20260901)
    children = []

    with open(args.events, "w") as fh:
        log(fh, event="start", pid=os.getpid(), config=vars(args))
        chunks = allocate_working_set(args.working_set_mb, fh)

        # The always-on single-threaded modelling kernel.
        kernel = subprocess.Popen(
            [args.burn, "geom", "--verts", "1500000", "--threads", "1",
             "--seconds", str(args.duration)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        children.append(kernel)
        log(fh, event="kernel_started", pid=kernel.pid)

        deadline = time.monotonic() + args.duration
        next_burst = time.monotonic() + args.burst_every
        next_save = time.monotonic() + args.autosave_every
        bursts = saves = 0

        try:
            while time.monotonic() < deadline:
                touch(chunks, rng, 20000)

                now = time.monotonic()
                if now >= next_burst and deadline - now > args.burst_seconds:
                    bursts += 1
                    log(fh, event="rebuild_start", n=bursts, threads=args.burst_threads)
                    p = subprocess.Popen(
                        [args.burn, "matmul", "--n", "640", "--threads",
                         str(args.burst_threads), "--seconds", str(args.burst_seconds)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    )
                    out, _ = p.communicate()
                    try:
                        log(fh, event="rebuild_done", n=bursts, result=json.loads(out))
                    except (json.JSONDecodeError, TypeError):
                        log(fh, event="rebuild_done", n=bursts, result=None)
                    next_burst = time.monotonic() + args.burst_every

                if time.monotonic() >= next_save:
                    saves += 1
                    free = shutil.disk_usage(args.scratch).free
                    if free < (args.autosave_mb + 512) * (1 << 20):
                        log(fh, event="autosave_skipped", reason="low disk space",
                            free_mib=free // (1 << 20))
                    else:
                        autosave(args.scratch, args.autosave_mb, rng, fh, saves)
                    next_save = time.monotonic() + args.autosave_every
        except KeyboardInterrupt:
            pass
        finally:
            for c in children:
                if c.poll() is None:
                    c.terminate()
                    try:
                        c.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        c.kill()
            log(fh, event="stop", rebuilds=bursts, autosaves=saves,
                working_set_mib=len(chunks) * CHUNK_MB)
    return 0


if __name__ == "__main__":
    sys.exit(main())
