# Laptop benchmark: CAD + web 3D + browser tabs

A benchmark and stress test that reproduces a real interior-design workstation
load — a CAD application with a large assembly open, a 3D simulator running in
the browser, and a handful of Google tabs — then reports whether the machine
actually copes.

It answers the question a spec sheet cannot: *does this laptop stay usable when
all three are running at once?*

## Quick start

```bash
cd tools/laptop-benchmark
./run.sh                      # ~15 min, the realistic workload
```

Results land in `lsbench-results/<timestamp>/`:

| File | Contents |
| --- | --- |
| `report.html` | Verdict, findings, headline numbers and timeline charts |
| `report.md` | The same report as Markdown, for pasting into a ticket |
| `report.json` | Every raw sample and measurement |
| `cad_events.jsonl` | The CAD emulator's own event log |

Other profiles:

```bash
./run.sh --profile quick      # ~3 min  - verify the setup works
./run.sh --profile soak       # ~50 min - exposes thermal decay a short run hides
./run.sh --profile max        # ~12 min - stability ceiling, not a realistic load
```

## Requirements

- macOS 12+ on Apple Silicon (primary target), or Linux
- Python 3.8+ (macOS ships this)
- A C compiler — `xcode-select --install` on macOS
- Google Chrome, Chromium, Edge or Brave for the browser workload

Nothing needs to be installed with `pip`. The tool is standard library only.

**Optional, for richer data on macOS:** pre-authorise sudo so `powermetrics` can
record package power and per-cluster frequency.

```bash
sudo -v && ./run.sh
```

Without it the run still measures throttling, via `pmset -g therm`.

## What it actually runs

### Phases

| Phase | Purpose |
| --- | --- |
| `idle` | Quiet baseline. If the machine was already busy, the report says so. |
| `baseline` | CPU, memory-bandwidth and storage microbenchmarks with nothing competing. |
| `stress` | All three applications at once. A contended CPU measurement is taken 60% of the way through. |
| `sustained` | The same CPU benchmark again, hot, immediately after the load stops. |
| `cooldown` | Telemetry only, to show recovery. |

### The workload

**CAD emulator** (`lsb/cadproc.py`) — a separate process, so its memory shows up
on its own in Activity Monitor. It reproduces the four things that make CAD hard
on a laptop:

1. A large resident working set (the model in memory), filled with
   page-level-incompressible data so macOS memory compression cannot fake it
   away. Sized at ~32% of installed RAM by default.
2. A continuously busy single thread. Geometry kernels such as Parasolid and
   ACIS are overwhelmingly single-threaded, which is why CAD rewards single-core
   speed far more than core count.
3. All-performance-core bursts every 20s — rebuild, tessellation, booleans.
4. A 200 MB fsync'd autosave every 60s, plus a reference read-back.

**Web 3D simulator** (`lsb/webassets/sim.html`) — a real WebGL scene: 40 shaded,
vertex-displaced, specular-lit objects redrawn every frame, about 1.3M triangles
per frame (3.3M on the `max` profile). It reports its own FPS, frame times and
jank count back to the harness, so the browser's actual achieved frame rate is
measured rather than inferred from CPU counters.

**Browser tabs** (`lsb/webassets/tab.html`) — an office tab mix (Gmail, Docs,
Sheets, Drive, Calendar, Meet…), each holding a realistic retained heap of
150–340 MB plus ~900 live DOM nodes that mutate on a timer and force real layout
and paint.

Chrome always runs in a throwaway `--user-data-dir`. **Your real profile,
session, cookies and extensions are never touched.**

Background tabs are left subject to Chrome's normal throttling, because real
background tabs are throttled too — their retained memory is what actually
competes with CAD. `--keep-tabs-active` disables that if you want the harsher
case.

### What is measured

- **CPU** — single-thread and all-thread fp32 throughput, and a multi-core
  scaling curve that exposes the P/E core split on Apple Silicon.
- **Responsiveness headroom** — the same single-thread benchmark run *during*
  the full workload. This is the number that predicts whether the UI feels
  laggy: it is what is left for whatever you do next.
- **Sustained performance** — the benchmark again immediately after the load,
  while the machine is hot, as a percentage of the cold baseline. On a laptop
  this gap matters more than peak speed.
- **Memory** — peak footprint, compressor growth, and swap growth. Swap growth
  is the single strongest signal that a machine is under-specified for CAD.
- **Thermal throttling** — `CPU_Speed_Limit` sampled throughout, plus package
  power and per-cluster frequency when `powermetrics` is available.
- **GPU** — utilisation from `ioreg`, and the simulator's achieved frame rate.
- **Storage** — sequential write, uncached sequential read and 4K random read
  IOPS. Reads use `F_NOCACHE` on macOS so they measure the SSD, not RAM.

The report turns these into a **PASS / MARGINAL / FAIL** verdict with specific
findings and a recommended specification.

## Useful options

```bash
./run.sh --duration 1800            # custom stress length, seconds
./run.sh --tabs 10                  # more background tabs
./run.sh --working-set-mb 12000     # a much larger CAD assembly
./run.sh --no-browser               # CAD load only
./run.sh --keep-tabs-active         # remove Chrome's background throttling
./run.sh --browser "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

`bench.py` exits non-zero when the verdict is FAIL, so it can gate a
procurement check in CI.

## Reading the result

The findings are ordered worst-first. In practice, for a CAD-class workload:

- **Swap growth over ~2 GB** means memory is the binding constraint. More cores
  will not help; more RAM will.
- **Sustained retention below ~90%** means the chassis cannot hold the clocks.
  This is the Air-versus-Pro distinction, and a short benchmark hides it — use
  `--profile soak` to confirm.
- **Responsiveness headroom below ~35%** means the machine is saturated and the
  interface will feel unresponsive, even if every individual benchmark looks
  respectable.

## Notes and limitations

- **The CAD emulator does not drive the GPU.** A real CAD viewport does. GPU
  load in this test comes from the web simulator only, so total GPU contention
  is understated relative to a real session.
- **It is an emulation, not SolidWorks.** It reproduces the resource *shape* of
  a CAD workload — single-thread-bound, memory-heavy, burst-parallel, periodic
  large writes. It cannot reproduce a specific application's inefficiencies.
- **Run on AC power.** macOS caps sustained performance on battery; `run.sh`
  warns and asks before continuing.
- **Close other applications first.** The `idle` phase detects a busy machine
  and flags it in the report, but clean numbers need a quiet start.
- On Linux, read benchmarks are not cache-bypassed and there is no hardware
  speed-limit signal, so throttling is reported as not measurable. The report
  states both rather than quietly reporting optimistic figures.

## Layout

```
bench.py               CLI driver and phase orchestration
run.sh                 prerequisite checks, then bench.py
lsb/
  kernels.c            native compute kernels (matmul, geom, STREAM triad)
  native.py            compiles and invokes them
  microbench.py        CPU / memory / storage suites
  sysinfo.py           hardware and OS inventory
  telemetry.py         background sampler (macOS and Linux backends)
  workload.py          concurrent workload orchestration
  cadproc.py           the CAD application emulator
  browser.py           local report server and Chrome launcher
  report.py            analysis, verdict, Markdown and HTML rendering
  webassets/
    sim.html           WebGL 3D simulator
    tab.html           heavy browser tab emulator
```
