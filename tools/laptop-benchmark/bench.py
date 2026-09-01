#!/usr/bin/env python3
"""
Laptop benchmark and stress test for a CAD + web-3D + browser-tabs workload.

Phases
  1. idle        quiet baseline, to prove the machine was not already busy
  2. baseline    CPU / memory-bandwidth / storage microbenchmarks, unloaded
  3. stress      CAD emulator + WebGL simulator + browser tabs, all at once
                 (a contended CPU measurement is taken partway through)
  4. sustained   the same CPU benchmark again, hot, immediately after the load
  5. cooldown    telemetry only, to show recovery

Everything is sampled throughout, and the result is a verdict plus an HTML and
Markdown report.

    python3 bench.py                      # ~15 min realistic workload
    python3 bench.py --profile quick      # ~3 min, to check the setup
    python3 bench.py --profile soak       # ~50 min, exposes thermal decay
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lsb import __version__, microbench, native, report, sysinfo  # noqa: E402
from lsb.browser import find_browser  # noqa: E402
from lsb.telemetry import Sampler  # noqa: E402
from lsb.util import human_bytes  # noqa: E402
from lsb.workload import Workload, default_working_set_mb  # noqa: E402

PROFILES = {
    "quick":     dict(idle=10,  stress=75,   cooldown=15, quick_bench=True,  tabs=4),
    "realistic": dict(idle=30,  stress=720,  cooldown=30, quick_bench=False, tabs=6),
    "soak":      dict(idle=30,  stress=2700, cooldown=60, quick_bench=False, tabs=6),
    "max":       dict(idle=20,  stress=600,  cooldown=60, quick_bench=False, tabs=8),
}


def hr(title=""):
    line = "-" * 68
    return f"\n{line}\n{title}\n{line}" if title else line


def build_config(args, si):
    p = PROFILES[args.profile]
    logical = si["logical_cpus"]
    burst = si.get("burst_threads") or max(1, logical // 2)
    ws = args.working_set_mb or default_working_set_mb(si.get("ram_bytes"))

    cfg = {
        "profile": args.profile,
        "tool_version": __version__,
        "idle_seconds": args.idle if args.idle is not None else p["idle"],
        "stress_seconds": args.duration if args.duration is not None else p["stress"],
        "cooldown_seconds": p["cooldown"],
        "quick_bench": p["quick_bench"],
        "sample_interval": args.interval,
        "tabs": p["tabs"] if args.tabs is None else args.tabs,
        "headless": args.headless,
        "keep_tabs_active": args.keep_tabs_active,
        "browser_path": args.browser,
        "browser_flags": list(args.browser_flag),
        "bench_threads": logical,
        "cad_working_set_mb": ws,
        "cad_burst_threads": burst,
        "cad_burst_every": 20.0,
        "cad_burst_seconds": 4.0,
        "cad_autosave_every": 60.0,
        "cad_autosave_mb": 200,
        "sim_objects": 40,
        "sim_segments": 128,
        "sim_res": 1.0,
    }
    if args.profile == "max":
        cfg.update(cad_burst_threads=logical, cad_burst_every=6.0, cad_burst_seconds=5.0,
                   cad_autosave_every=30.0, cad_autosave_mb=400,
                   sim_objects=64, sim_segments=160,
                   cad_working_set_mb=args.working_set_mb or int(ws * 1.4))
    if args.profile == "quick":
        cfg.update(cad_burst_every=12.0, cad_burst_seconds=3.0,
                   cad_autosave_every=25.0, cad_autosave_mb=80,
                   cad_working_set_mb=args.working_set_mb or min(ws, 1536))
    if args.no_browser:
        cfg["tabs"] = 0
    return cfg


def preflight(cfg, si, scratch):
    print(hr("Preflight"))
    problems, warnings = [], []

    try:
        binary = native.build(os.path.join(scratch, "build"))
        print(f"  compiler        : ok -> {binary}")
    except native.NativeUnavailable as exc:
        problems.append(str(exc))
        binary = None
        print(f"  compiler        : MISSING - {exc}")

    if cfg["tabs"] or not cfg.get("no_browser"):
        b = find_browser(cfg.get("browser_path"))
        print(f"  browser         : {b or 'NOT FOUND (web workload will be skipped)'}")
        if not b:
            warnings.append("No Chromium-family browser found; 3D and tab load will be skipped.")

    free = shutil.disk_usage(scratch).free
    need = (cfg["cad_autosave_mb"] * 3 + 2048) * (1 << 20)
    print(f"  scratch space   : {human_bytes(free)} free at {scratch}")
    if free < need:
        warnings.append(f"Only {human_bytes(free)} free; needs about {human_bytes(need)}.")

    ram = si.get("ram_bytes") or 0
    print(f"  CAD assembly    : {cfg['cad_working_set_mb']} MiB "
          f"({100.0 * cfg['cad_working_set_mb'] * (1 << 20) / ram:.0f}% of RAM)" if ram
          else f"  CAD assembly    : {cfg['cad_working_set_mb']} MiB")

    batt = si.get("battery") or {}
    if batt.get("power_source") == "Battery":
        warnings.append("Running on battery - macOS will cap sustained performance. "
                        "Plug in for representative numbers.")
    if batt.get("power_source"):
        print(f"  power source    : {batt['power_source']}")

    est = (cfg["idle_seconds"] + cfg["stress_seconds"] + cfg["cooldown_seconds"]
           + (60 if cfg["quick_bench"] else 130))
    print(f"  estimated time  : {est / 60:.0f} min")

    for w in warnings:
        print(f"  ! {w}")
    if problems:
        for p in problems:
            print(f"  x {p}")
        return None, warnings
    return binary, warnings


def sleep_phase(sampler, name, seconds, label):
    sampler.set_phase(name)
    sampler.mark(f"{name} start")
    print(f"\n[{name}] {label} ({seconds:.0f}s)")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(5.0, max(0.1, end - time.monotonic())))
        remain = max(0, end - time.monotonic())
        last = sampler.samples[-1] if sampler.samples else {}
        print(f"\r  {remain:5.0f}s left   cpu {last.get('cpu_busy', 0):5.1f}%   "
              f"mem {human_bytes(last.get('mem_used_bytes')):>9}", end="", flush=True)
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Benchmark and stress-test a laptop for CAD + web 3D + browser tabs.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="realistic")
    ap.add_argument("--duration", type=float, help="override the stress phase length, seconds")
    ap.add_argument("--idle", type=float, help="override the idle baseline length, seconds")
    ap.add_argument("--tabs", type=int, help="number of background browser tabs")
    ap.add_argument("--working-set-mb", type=int, help="size of the emulated CAD assembly")
    ap.add_argument("--interval", type=float, default=2.0, help="telemetry sample interval")
    ap.add_argument("--browser", help="path to a Chromium-family browser binary")
    ap.add_argument("--browser-flag", action="append", default=[], metavar="FLAG",
                    help="extra flag to pass to the browser; repeatable. Used by CI to force "
                         "software GL (--browser-flag=--use-angle=swiftshader); do not use on "
                         "a real laptop, it would hide a genuine GPU failure")
    ap.add_argument("--headless", action="store_true",
                    help="run the browser headless (CI only - loses real GPU measurement)")
    ap.add_argument("--keep-tabs-active", action="store_true",
                    help="disable Chrome background-tab throttling (less realistic, more load)")
    ap.add_argument("--no-browser", action="store_true", help="CAD workload only")
    ap.add_argument("--output", default="lsbench-results", help="results directory")
    ap.add_argument("--keep-scratch", action="store_true")
    args = ap.parse_args(argv)

    print(hr(f"lsbench {__version__} - laptop benchmark for CAD-class workloads"))
    print("\nCollecting system information...")
    scratch = tempfile.mkdtemp(prefix="lsbench-")
    si = sysinfo.collect(scratch)
    print("\n".join("  " + l for l in sysinfo.summary_lines(si)))

    cfg = build_config(args, si)
    cfg["no_browser"] = args.no_browser
    binary, warnings = preflight(cfg, si, scratch)
    if binary is None:
        print("\nCannot continue without a C compiler.")
        shutil.rmtree(scratch, ignore_errors=True)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(args.output, stamp)
    os.makedirs(outdir, exist_ok=True)

    run = {
        "tool_version": __version__,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "sysinfo": si,
        "preflight_warnings": warnings,
    }

    sampler = Sampler(interval=cfg["sample_interval"]).start()
    workload = None
    interrupted = False

    def handle_sigint(_sig, _frm):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        # 1 -- idle
        sleep_phase(sampler, "idle", cfg["idle_seconds"],
                    "measuring the quiet baseline - leave the machine alone")

        # 2 -- unloaded microbenchmarks
        sampler.set_phase("baseline")
        print(f"\n[baseline] unloaded microbenchmarks")
        print("  cpu...", end="", flush=True)
        cpu = microbench.cpu_suite(binary, cfg["bench_threads"], quick=cfg["quick_bench"])
        print(f" single {cpu['single_thread'].get('gflops', 0):.1f} GFLOP/s, "
              f"all-core {cpu['all_threads'].get('gflops', 0):.1f} GFLOP/s "
              f"({cpu.get('multicore_scaling')}x)")
        print("  memory...", end="", flush=True)
        mem = microbench.memory_suite(binary, cfg["bench_threads"], quick=cfg["quick_bench"])
        print(f" {mem['triad_all'].get('gb_per_s', 0):.1f} GB/s triad")
        print("  disk...", end="", flush=True)
        disk = microbench.disk_suite(os.path.join(scratch, "disk"), quick=cfg["quick_bench"])
        print(f" write {disk.get('seq_write_mb_s', 0):.0f} MB/s, "
              f"random read {disk.get('rand_read_iops', 0)} IOPS")
        run["baseline"] = {"cpu": cpu, "memory": mem, "disk": disk}

        # 3 -- the real thing
        sampler.set_phase("stress")
        print(f"\n[stress] CAD + web 3D + {cfg['tabs']} tabs for "
              f"{cfg['stress_seconds'] / 60:.1f} min")
        workload = Workload(cfg, binary, scratch, sampler, log=print)
        state = {"contended": None}
        t_contend = cfg["stress_seconds"] * 0.6

        def on_tick(elapsed, total):
            last = sampler.samples[-1] if sampler.samples else {}
            sims = [r for r in (workload.server.of_kind("sim") if workload.server else [])
                    if r.get("fps") is not None]
            fps = f"{sims[-1]['fps']:5.1f}" if sims else "  n/a"
            swap = human_bytes(last.get("swap_used_bytes"))
            print(f"  {elapsed:5.0f}/{total:.0f}s  cpu {last.get('cpu_busy', 0):5.1f}%  "
                  f"mem {human_bytes(last.get('mem_used_bytes')):>9}  swap {swap:>9}  "
                  f"fps {fps}  limit {last.get('cpu_speed_limit', '-')}")
            if state["contended"] is None and elapsed >= t_contend:
                print("  --> measuring responsiveness headroom under full load...")
                sampler.mark("contended benchmark")
                state["contended"] = microbench.sustained_check(
                    binary, cfg["bench_threads"], seconds=5.0)
                g = state["contended"]["single_thread"].get("gflops", 0)
                print(f"      single-thread under load: {g:.1f} GFLOP/s")

        run["workload"] = workload.run(cfg["stress_seconds"], on_tick=on_tick, tick_every=15.0)
        run["contended"] = state["contended"] or {}

        print("\n  tearing down workload...")
        workload.stop()
        workload = None

        # 4 -- hot re-measurement
        sampler.set_phase("sustained")
        print("\n[sustained] re-running the CPU benchmark while the machine is hot")
        run["sustained"] = microbench.sustained_check(binary, cfg["bench_threads"], seconds=6.0)
        base_st = (run["baseline"]["cpu"]["single_thread"].get("gflops") or 0)
        hot_st = (run["sustained"]["single_thread"].get("gflops") or 0)
        if base_st:
            print(f"  single-thread hot: {hot_st:.1f} GFLOP/s "
                  f"({100.0 * hot_st / base_st:.0f}% of cold baseline)")

        # 5 -- cooldown
        sleep_phase(sampler, "cooldown", cfg["cooldown_seconds"], "recording recovery")

    except KeyboardInterrupt:
        interrupted = True
        print("\n\nInterrupted - shutting the workload down cleanly...")
    finally:
        if workload:
            try:
                run["workload"] = workload.collect()
            except Exception:
                pass
            workload.stop()
        run["samples"] = sampler.stop()
        run["sampler_notes"] = sampler.notes

    run.setdefault("workload", {"problems": [], "sim_reports": [], "tab_reports": [], "cad": {}})
    run.setdefault("baseline", {"cpu": {}, "memory": {}, "disk": {}})
    run.setdefault("contended", {})
    run.setdefault("sustained", {})
    run["interrupted"] = interrupted
    if interrupted:
        run["workload"].setdefault("problems", []).append(
            "Run was interrupted before completing; results are partial.")

    print(hr("Analysing"))
    a = report.analyse(run)
    overall, findings = report.verdict(a, run)
    recs = report.recommendations(a, findings)

    md = report.to_markdown(run, a, overall, findings, recs)
    html_doc = report.to_html(run, a, overall, findings, recs)
    with open(os.path.join(outdir, "report.md"), "w") as fh:
        fh.write(md)
    with open(os.path.join(outdir, "report.html"), "w") as fh:
        fh.write(html_doc)
    with open(os.path.join(outdir, "report.json"), "w") as fh:
        json.dump({"run": run, "analysis": a,
                   "verdict": {"overall": overall,
                               "findings": [{"severity": s, "title": t, "detail": d}
                                            for s, t, d in findings],
                               "recommendations": recs}}, fh, indent=1, default=str)
    cad_events = os.path.join(scratch, "cad_events.jsonl")
    if os.path.exists(cad_events):
        shutil.copy(cad_events, os.path.join(outdir, "cad_events.jsonl"))

    print(md)
    print(hr())
    print(f"HTML report : {os.path.abspath(os.path.join(outdir, 'report.html'))}")
    print(f"Markdown    : {os.path.abspath(os.path.join(outdir, 'report.md'))}")
    print(f"Raw data    : {os.path.abspath(os.path.join(outdir, 'report.json'))}")

    if args.keep_scratch:
        print(f"Scratch kept: {scratch}")
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if overall == report.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
