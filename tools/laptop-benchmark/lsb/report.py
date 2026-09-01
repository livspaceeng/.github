"""Analysis, verdict and report rendering (Markdown + self-contained HTML)."""
from __future__ import annotations

import html
import json
import time

from .util import human_bytes, percentile

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
GIB = 1024 ** 3


def _vals(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def _phase(samples, name):
    return [s for s in samples if s.get("phase") == name]


def _delta(samples, key):
    v = _vals(samples, key)
    return (max(v) - min(v)) if len(v) >= 2 else None


# ---------------------------------------------------------------- analysis

def analyse(run):
    samples = run["samples"]
    idle = _phase(samples, "idle")
    stress = _phase(samples, "stress")
    a = {"idle": {}, "stress": {}, "cad": {}, "web": {}, "cpu": {}, "memory": {}, "thermal": {}}

    # --- idle sanity: was the machine actually quiet before we started?
    idle_cpu = _vals(idle, "cpu_busy")
    a["idle"]["cpu_busy_p50"] = percentile(idle_cpu, 50)
    a["idle"]["mem_used_bytes"] = percentile(_vals(idle, "mem_used_bytes"), 50)
    a["idle"]["swap_used_bytes"] = percentile(_vals(idle, "swap_used_bytes"), 50)

    # --- CPU under load
    busy = _vals(stress, "cpu_busy")
    a["cpu"]["busy_p50"] = percentile(busy, 50)
    a["cpu"]["busy_p95"] = percentile(busy, 95)
    a["cpu"]["load1_max"] = max(_vals(stress, "load1"), default=None)

    base = run.get("baseline", {}).get("cpu", {})
    sust = run.get("sustained", {})
    cont = run.get("contended", {})

    def gf(d, k):
        return (d.get(k) or {}).get("gflops")

    a["cpu"]["baseline_st_gflops"] = gf(base, "single_thread")
    a["cpu"]["baseline_mt_gflops"] = gf(base, "all_threads")
    a["cpu"]["sustained_st_gflops"] = gf(sust, "single_thread")
    a["cpu"]["contended_st_gflops"] = gf(cont, "single_thread")
    a["cpu"]["multicore_scaling"] = base.get("multicore_scaling")

    if a["cpu"]["baseline_st_gflops"] and a["cpu"]["sustained_st_gflops"]:
        a["cpu"]["sustained_retention_pct"] = round(
            100.0 * a["cpu"]["sustained_st_gflops"] / a["cpu"]["baseline_st_gflops"], 1)
    if a["cpu"]["baseline_st_gflops"] and a["cpu"]["contended_st_gflops"]:
        a["cpu"]["contended_headroom_pct"] = round(
            100.0 * a["cpu"]["contended_st_gflops"] / a["cpu"]["baseline_st_gflops"], 1)

    # --- thermal / throttle
    limits = _vals(stress, "cpu_speed_limit")
    if limits:
        throttled = [x for x in limits if x < 100]
        a["thermal"]["speed_limit_min"] = min(limits)
        a["thermal"]["throttled_pct_of_time"] = round(100.0 * len(throttled) / len(limits), 1)
        a["thermal"]["measured"] = True
    else:
        a["thermal"]["measured"] = False
    a["thermal"]["temp_c_max"] = max(_vals(stress, "temp_c"), default=None)
    a["thermal"]["package_power_w_max"] = max(_vals(stress, "package_power_w"), default=None)
    a["thermal"]["p_cluster_mhz_min"] = min(_vals(stress, "p_cluster_mhz"), default=None)
    pressures = [s.get("thermal_pressure") for s in stress if s.get("thermal_pressure")]
    if pressures:
        a["thermal"]["pressure_levels"] = sorted(set(pressures))

    # --- memory
    ram = (run.get("sysinfo") or {}).get("ram_bytes")
    peak_used = max(_vals(stress, "mem_used_bytes"), default=None)
    a["memory"]["ram_bytes"] = ram
    a["memory"]["peak_used_bytes"] = peak_used
    a["memory"]["peak_used_pct"] = round(100.0 * peak_used / ram, 1) if (ram and peak_used) else None
    swap_stress = _vals(stress, "swap_used_bytes")
    idle_swap = a["idle"]["swap_used_bytes"] or 0
    a["memory"]["peak_swap_bytes"] = max(swap_stress, default=None)
    if swap_stress:
        a["memory"]["swap_growth_bytes"] = max(0.0, max(swap_stress) - idle_swap)
    a["memory"]["compressed_peak_bytes"] = max(_vals(stress, "mem_compressed_bytes"), default=None)
    a["memory"]["swapouts_delta"] = _delta(samples, "swapouts")
    a["memory"]["pageouts_delta"] = _delta(samples, "pageouts")

    # --- web 3D
    sim = [r for r in run.get("workload", {}).get("sim_reports", []) if r.get("fps") is not None]
    if sim:
        fps = [r["fps"] for r in sim]
        a["web"]["fps_p50"] = round(percentile(fps, 50), 1)
        a["web"]["fps_p05"] = round(percentile(fps, 5), 1)
        a["web"]["fps_min"] = round(min(fps), 1)
        a["web"]["frame_ms_p95"] = round(percentile([r.get("frame_ms_p95") or 0 for r in sim], 95), 1)
        a["web"]["jank_frames"] = sum(r.get("jank_frames") or 0 for r in sim)
        a["web"]["frames"] = max((r.get("frames") or 0) for r in sim)
        a["web"]["tris_per_frame"] = sim[-1].get("tris_per_frame")
        a["web"]["seconds_sampled"] = len(sim)
    ready = next((r for r in run.get("workload", {}).get("sim_reports", [])
                  if r.get("event") == "ready"), {})
    a["web"]["renderer"] = ready.get("renderer")
    a["web"]["software_rendered"] = bool(
        ready.get("renderer") and any(
            t in ready["renderer"].lower() for t in ("swiftshader", "llvmpipe", "software")))
    errs = [r.get("error") for r in run.get("workload", {}).get("sim_reports", []) if r.get("error")]
    a["web"]["errors"] = errs

    tabs = run.get("workload", {}).get("tab_reports", [])
    by_tab = {}
    for r in tabs:
        by_tab[r.get("name")] = r
    a["web"]["tabs_reporting"] = len(by_tab)
    a["web"]["tab_heap_mb_total"] = round(
        sum((r.get("js_heap_mb") or 0) for r in by_tab.values()), 1) or None

    # --- CAD
    cad = run.get("workload", {}).get("cad", {})
    a["cad"].update(cad)
    gflops = cad.get("rebuild_gflops") or []
    if len(gflops) >= 2:
        a["cad"]["rebuild_decay_pct"] = round(100.0 * (1 - gflops[-1] / gflops[0]), 1)

    a["disk"] = run.get("baseline", {}).get("disk", {})
    a["memory_bw"] = run.get("baseline", {}).get("memory", {})
    return a


# ---------------------------------------------------------------- verdict

def _f(x, unit="", nd=1):
    return "n/a" if x is None else f"{x:.{nd}f}{unit}"


def verdict(a, run):
    """Return (overall, findings). Each finding: (severity, title, detail)."""
    f = []
    ram_gb = (a["memory"].get("ram_bytes") or 0) / GIB
    sysinfo = run.get("sysinfo") or {}

    # Environment caveats first - they change how everything else reads.
    idle_cpu = a["idle"].get("cpu_busy_p50")
    if idle_cpu is not None and idle_cpu > 15:
        f.append((WARN, "Machine was not idle before the run",
                  f"Baseline CPU was {idle_cpu:.0f}% busy before the workload started. "
                  "Other applications were competing; close them and re-run for clean numbers."))
    batt = sysinfo.get("battery") or {}
    if batt.get("power_source") == "Battery":
        f.append((WARN, "Ran on battery power",
                  "macOS restricts sustained performance on battery. Re-run on AC power "
                  "for numbers that reflect desk use."))
    if batt.get("condition") and batt["condition"] not in ("Normal", "Good"):
        f.append((WARN, f"Battery condition: {batt['condition']}",
                  "A degraded battery can limit peak power delivery under load."))

    # Memory - the usual failure mode for CAD on a laptop.
    swap_growth = a["memory"].get("swap_growth_bytes")
    peak_pct = a["memory"].get("peak_used_pct")
    if swap_growth is not None and swap_growth > 2 * GIB:
        f.append((FAIL, "Memory exhausted - heavy swapping",
                  f"Swap grew by {human_bytes(swap_growth)} while the workload ran. "
                  f"{ram_gb:.0f} GB is not enough for this combination; the machine was paging "
                  "to SSD, which is what users experience as beachballing."))
    elif swap_growth is not None and swap_growth > 512 * 1024 ** 2:
        f.append((WARN, "Memory under pressure - some swapping",
                  f"Swap grew by {human_bytes(swap_growth)}. Headroom is thin: a larger "
                  "assembly or a few more tabs will push this into sustained paging."))
    elif peak_pct is not None and peak_pct > 90:
        f.append((WARN, "Memory nearly full",
                  f"Peak memory use hit {peak_pct:.0f}% of {ram_gb:.0f} GB without swapping yet."))
    elif swap_growth is not None:
        f.append((OK, "Memory headroom is adequate",
                  f"Peak use {human_bytes(a['memory'].get('peak_used_bytes'))} "
                  f"({_f(peak_pct, '%', 0)} of RAM), swap growth {human_bytes(swap_growth)}."))

    # Thermals / sustained performance.
    retention = a["cpu"].get("sustained_retention_pct")
    thr = a["thermal"].get("throttled_pct_of_time")
    if thr is not None and thr > 25:
        f.append((WARN, "Sustained thermal throttling",
                  f"The CPU speed limit was below 100% for {thr:.0f}% of the run "
                  f"(minimum {a['thermal'].get('speed_limit_min')}%)."))
    elif thr is not None and thr > 0:
        f.append((INFO, "Brief thermal throttling",
                  f"Speed limit dipped below 100% for {thr:.0f}% of the run."))
    elif a["thermal"].get("measured"):
        f.append((OK, "No thermal throttling detected",
                  "The CPU speed limit stayed at 100% for the whole run."))

    if retention is not None:
        if retention < 80:
            f.append((FAIL, "Poor sustained performance",
                      f"Single-thread throughput after the workload was {retention:.0f}% of the "
                      "cold baseline. Long CAD sessions will get progressively slower."))
        elif retention < 90:
            f.append((WARN, "Noticeable performance decay when hot",
                      f"Single-thread throughput fell to {retention:.0f}% of the cold baseline."))
        else:
            f.append((OK, "Performance holds up when hot",
                      f"Single-thread throughput stayed at {retention:.0f}% of the cold baseline."))

    # Responsiveness headroom - can the user still do anything?
    headroom = a["cpu"].get("contended_headroom_pct")
    if headroom is not None:
        if headroom < 35:
            f.append((FAIL, "No responsiveness headroom",
                      f"With the full workload running, a new single-threaded task ran at only "
                      f"{headroom:.0f}% of its unloaded speed. The UI will feel unresponsive."))
        elif headroom < 55:
            f.append((WARN, "Limited responsiveness headroom",
                      f"A new task ran at {headroom:.0f}% of unloaded speed under full load."))
        else:
            f.append((OK, "Good responsiveness under load",
                      f"A new task still ran at {headroom:.0f}% of unloaded speed."))

    # Web 3D experience.
    if a["web"].get("software_rendered"):
        f.append((WARN, "Browser fell back to software rendering",
                  f"Renderer reported as {a['web'].get('renderer')}. GPU acceleration was not "
                  "in use, so the FPS figures reflect the CPU, not the GPU. On a real desktop "
                  "session with hardware acceleration enabled, expect far higher numbers."))
    fps = a["web"].get("fps_p50")
    if fps is not None and not a["web"].get("software_rendered"):
        if fps < 24:
            f.append((FAIL, "Web 3D simulator unusable under load",
                      f"Median {fps:.0f} FPS (1% low {a['web'].get('fps_p05')}) with CAD and tabs "
                      "running. Interactive 3D on the web will stutter badly."))
        elif fps < 45:
            f.append((WARN, "Web 3D simulator degraded under load",
                      f"Median {fps:.0f} FPS (1% low {a['web'].get('fps_p05')})."))
        else:
            f.append((OK, "Web 3D simulator stays smooth",
                      f"Median {fps:.0f} FPS (1% low {a['web'].get('fps_p05')}) alongside "
                      "the CAD workload."))
    if a["web"].get("errors"):
        f.append((WARN, "3D simulator reported errors", "; ".join(a["web"]["errors"][:3])))

    # Storage - autosave stalls are a real CAD complaint.
    disk = a.get("disk") or {}
    w = disk.get("seq_write_mb_s")
    if w is not None:
        if w < 400:
            f.append((WARN, "Slow sustained write throughput",
                      f"{w:.0f} MB/s sequential write. Large autosaves will stall the UI."))
        else:
            f.append((OK, "Storage write throughput is fine",
                      f"{w:.0f} MB/s sequential write, "
                      f"{disk.get('rand_read_iops', 'n/a')} random 4K read IOPS."))
    if disk.get("cache_bypassed") is False:
        f.append((INFO, "Read figures include OS cache",
                  "Cache bypass is only implemented for macOS, so sequential and random read "
                  "numbers on this platform are optimistic."))

    # Problems raised by the harness itself.
    for p in run.get("workload", {}).get("problems", []):
        f.append((WARN, "Workload component problem", p))

    order = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}
    f.sort(key=lambda x: order[x[0]])
    overall = FAIL if any(x[0] == FAIL for x in f) else (
        WARN if any(x[0] == WARN for x in f) else OK)
    return overall, f


VERDICT_TEXT = {
    OK: ("PASS", "This machine handles the CAD + web 3D + tabs workload comfortably."),
    WARN: ("MARGINAL", "This machine runs the workload, but with real compromises."),
    FAIL: ("FAIL", "This machine is not adequate for this workload."),
}


def recommendations(a, findings):
    recs = []
    ram_gb = (a["memory"].get("ram_bytes") or 0) / GIB
    sev = {t: s for s, t, _ in findings}
    swap = a["memory"].get("swap_growth_bytes") or 0
    if swap > 2 * GIB:
        target = 32 if ram_gb <= 16 else 64
        recs.append(f"Specify at least {target} GB of unified memory for this role. "
                    f"Memory, not CPU, is the binding constraint on this machine.")
    elif swap > 512 * 1024 ** 2:
        recs.append("Keep the tab count down during CAD sessions, or move to the next memory "
                    "tier for new machines in this role.")
    if (a["cpu"].get("sustained_retention_pct") or 100) < 90:
        recs.append("Sustained load causes measurable clock decay - prefer an actively cooled "
                    "chassis (MacBook Pro rather than Air) for all-day CAD work.")
    if (a["cpu"].get("contended_headroom_pct") or 100) < 55:
        recs.append("The workload saturates the machine. Closing the web 3D simulator while "
                    "modelling will recover most of the responsiveness.")
    if a["web"].get("software_rendered"):
        recs.append("Re-run in a normal local desktop session with hardware acceleration "
                    "available - not headless, not over remote desktop. The current FPS "
                    "figures came from a software rasteriser and say nothing about the GPU.")
    if (a["disk"].get("seq_write_mb_s") or 1e9) < 400:
        recs.append("Increase the CAD autosave interval, or move project files to faster storage.")
    if not recs:
        recs.append("No changes needed - this configuration is a good fit for the workload.")
    return recs


# ---------------------------------------------------------------- charts

def _svg_chart(series, width=760, height=170, ylabel="", ymax=None, bands=None):
    """Minimal dependency-free line chart. `series` = [(label, [(t, v)...], colour)]."""
    pad_l, pad_r, pad_t, pad_b = 52, 12, 12, 24
    pts = [p for _, data, _ in series for p in data]
    if not pts:
        return '<p class="muted">no data</p>'
    xmax = max(t for t, _ in pts) or 1.0
    ymax = ymax or (max(v for _, v in pts) or 1.0)
    ymax *= 1.1
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b

    def X(t):
        return pad_l + iw * (t / xmax)

    def Y(v):
        return pad_t + ih * (1 - min(v, ymax) / ymax)

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'aria-label="{html.escape(ylabel)}">']
    for b in (bands or []):
        out.append(f'<rect x="{X(b["from"]):.1f}" y="{pad_t}" '
                   f'width="{max(1, X(b["to"]) - X(b["from"])):.1f}" height="{ih}" '
                   f'fill="{b["colour"]}" opacity="0.10"/>')
        # Only label a band wide enough to hold the text, or it collides
        # with its neighbour.
        if X(b["to"]) - X(b["from"]) > 7 * len(b["label"]):
            out.append(f'<text x="{X(b["from"]) + 4:.1f}" y="{pad_t + 11}" class="band">'
                       f'{html.escape(b["label"])}</text>')
    nd = 0 if ymax >= 20 else (1 if ymax >= 2 else 2)
    for i in range(5):
        y = pad_t + ih * i / 4
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">'
                   f'{ymax * (1 - i / 4):.{nd}f}</text>')
    for label, data, colour in series:
        if not data:
            continue
        d = " ".join(("M" if i == 0 else "L") + f"{X(t):.1f},{Y(v):.1f}"
                     for i, (t, v) in enumerate(data))
        out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="1.8" '
                   f'stroke-linejoin="round"/>')
    for i in range(5):
        x = pad_l + iw * i / 4
        out.append(f'<text x="{x:.1f}" y="{height - 6}" class="tick" text-anchor="middle">'
                   f'{xmax * i / 4:.0f}s</text>')
    out.append(f'<text x="4" y="{pad_t + 8}" class="tick">{html.escape(ylabel)}</text>')
    out.append("</svg>")
    legend = " ".join(
        f'<span class="key"><i style="background:{c}"></i>{html.escape(l)}</span>'
        for l, d, c in series if d)
    return "".join(out) + f'<div class="legend">{legend}</div>'


PHASE_COLOURS = {"idle": "#6e7781", "baseline": "#0969da", "stress": "#bf3989",
                 "sustained": "#9a6700", "cooldown": "#1a7f37"}


def _bands(samples):
    bands, current, start = [], None, 0.0
    for s in samples:
        ph = s.get("phase")
        if ph != current:
            if current is not None:
                bands.append({"label": current, "from": start, "to": s["t"],
                              "colour": PHASE_COLOURS.get(current, "#888")})
            current, start = ph, s["t"]
    if current is not None and samples:
        bands.append({"label": current, "from": start, "to": samples[-1]["t"],
                      "colour": PHASE_COLOURS.get(current, "#888")})
    return [b for b in bands if b["to"] - b["from"] > 1]


# ---------------------------------------------------------------- markdown

def to_markdown(run, a, overall, findings, recs):
    si = run.get("sysinfo", {})
    label, blurb = VERDICT_TEXT[overall]
    icon = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", INFO: "INFO"}
    L = []
    L.append(f"# Laptop benchmark - CAD + web 3D + browser tabs\n")
    L.append(f"**Verdict: {label}** - {blurb}\n")
    L.append(f"_Run {run['started_at']}, profile `{run['config']['profile']}`, "
             f"workload {run['config']['stress_seconds']}s._\n")

    L.append("## Machine\n")
    from .sysinfo import summary_lines
    L.append("```\n" + "\n".join(summary_lines(si)) + "\n```\n")

    L.append("## Findings\n")
    for sev, title, detail in findings:
        L.append(f"- **[{icon[sev]}] {title}** - {detail}")
    L.append("")

    L.append("## Recommendations\n")
    for r in recs:
        L.append(f"- {r}")
    L.append("")

    L.append("## Headline numbers\n")
    L.append("| Metric | Value |")
    L.append("| --- | --- |")
    rows = [
        ("CPU single-thread (cold)", _f(a["cpu"].get("baseline_st_gflops"), " GFLOP/s", 1)),
        ("CPU all-thread (cold)", _f(a["cpu"].get("baseline_mt_gflops"), " GFLOP/s", 1)),
        ("Multi-core scaling", _f(a["cpu"].get("multicore_scaling"), "x", 2)),
        ("Single-thread under full load", _f(a["cpu"].get("contended_st_gflops"), " GFLOP/s", 1)
         + f" ({_f(a['cpu'].get('contended_headroom_pct'), '%', 0)} of cold)"),
        ("Single-thread after load (hot)", _f(a["cpu"].get("sustained_st_gflops"), " GFLOP/s", 1)
         + f" ({_f(a['cpu'].get('sustained_retention_pct'), '%', 0)} of cold)"),
        ("Memory bandwidth", _f((a["memory_bw"].get("triad_all") or {}).get("gb_per_s"), " GB/s", 1)),
        ("CPU busy during workload", f"p50 {_f(a['cpu'].get('busy_p50'), '%', 0)}, "
                                     f"p95 {_f(a['cpu'].get('busy_p95'), '%', 0)}"),
        ("Peak memory used", human_bytes(a["memory"].get("peak_used_bytes"))
         + f" ({_f(a['memory'].get('peak_used_pct'), '%', 0)} of RAM)"),
        ("Swap growth during workload", human_bytes(a["memory"].get("swap_growth_bytes"))),
        ("Thermal throttling", (f"{_f(a['thermal'].get('throttled_pct_of_time'), '%', 0)} of run, "
                                f"floor {a['thermal'].get('speed_limit_min', 'n/a')}%")
         if a["thermal"].get("measured") else "not measurable on this platform"),
        ("Web 3D median FPS", _f(a["web"].get("fps_p50"), "", 1)
         + f" (1% low {_f(a['web'].get('fps_p05'), '', 1)})"),
        ("Web 3D geometry load", f"{(a['web'].get('tris_per_frame') or 0) / 1e6:.2f}M tris/frame"),
        ("CAD rebuilds completed", str(a["cad"].get("rebuilds", 0))),
        ("CAD assembly resident", f"{a['cad'].get('working_set_mib', 'n/a')} MiB"),
        ("Disk sequential write", _f(a["disk"].get("seq_write_mb_s"), " MB/s", 0)),
        ("Disk random 4K read", f"{a['disk'].get('rand_read_iops', 'n/a')} IOPS"),
    ]
    for k, v in rows:
        L.append(f"| {k} | {v} |")
    L.append("")

    curve = run.get("baseline", {}).get("cpu", {}).get("scaling_curve")
    if curve:
        L.append("## Multi-core scaling\n")
        L.append("| Threads | GFLOP/s | Speed-up |")
        L.append("| --- | --- | --- |")
        first = next((c["gflops"] for c in curve if c.get("gflops")), None)
        for c in curve:
            sp = f"{c['gflops'] / first:.2f}x" if (c.get("gflops") and first) else "n/a"
            L.append(f"| {c['threads']} | {_f(c.get('gflops'), '', 1)} | {sp} |")
        L.append("")

    if run.get("workload", {}).get("problems"):
        L.append("## Caveats\n")
        for p in run["workload"]["problems"]:
            L.append(f"- {p}")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--card:#f6f8fa;
 --ok:#1a7f37;--warn:#9a6700;--fail:#cf222e;--info:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
 --line:#3d444d;--card:#151b23;--ok:#3fb950;--warn:#d29922;--fail:#f85149;--info:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:18px;margin:36px 0 12px;
 padding-bottom:6px;border-bottom:1px solid var(--line)}
.muted{color:var(--muted)} .lead{color:var(--muted);margin:0 0 24px}
.verdict{padding:18px 20px;border-radius:10px;border:1px solid var(--line);
 background:var(--card);margin:0 0 8px;display:flex;gap:16px;align-items:center}
.badge{font-weight:700;font-size:15px;letter-spacing:.04em;padding:6px 14px;border-radius:999px;
 color:#fff;white-space:nowrap}
.badge.ok{background:var(--ok)}.badge.warn{background:var(--warn)}.badge.fail{background:var(--fail)}
table{border-collapse:collapse;width:100%;font-size:14px;display:block;overflow-x:auto}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}
td:last-child{font-variant-numeric:tabular-nums}
.finding{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}
.tag{font-size:11px;font-weight:700;letter-spacing:.05em;padding:3px 8px;border-radius:5px;
 height:fit-content;color:#fff;min-width:52px;text-align:center}
.tag.ok{background:var(--ok)}.tag.warn{background:var(--warn)}
.tag.fail{background:var(--fail)}.tag.info{background:var(--info)}
.finding b{display:block}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:14px 12px 8px;margin:14px 0}
.chart h3{margin:0 0 4px;font-size:14px;padding-left:6px}
.grid{stroke:var(--line);stroke-width:1}
.tick{fill:var(--muted);font-size:10px}
.band{fill:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em}
.legend{font-size:12px;color:var(--muted);padding:2px 6px 0;display:flex;gap:14px;flex-wrap:wrap}
.key i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;
 overflow-x:auto;font-size:13px}
ul{padding-left:20px}
"""


def to_html(run, a, overall, findings, recs):
    from .sysinfo import summary_lines
    si = run.get("sysinfo", {})
    label, blurb = VERDICT_TEXT[overall]
    samples = run["samples"]
    bands = _bands(samples)

    def pairs(key):
        return [(s["t"], s[key]) for s in samples if s.get(key) is not None]

    charts = []
    charts.append(("CPU utilisation", _svg_chart(
        [("CPU busy %", pairs("cpu_busy"), "#bf3989")], ylabel="%", ymax=100, bands=bands)))

    mem_series = [("Memory used (GiB)",
                   [(t, v / GIB) for t, v in pairs("mem_used_bytes")], "#0969da")]
    if pairs("swap_used_bytes"):
        mem_series.append(("Swap used (GiB)",
                           [(t, v / GIB) for t, v in pairs("swap_used_bytes")], "#cf222e"))
    if pairs("mem_compressed_bytes"):
        mem_series.append(("Compressed (GiB)",
                           [(t, v / GIB) for t, v in pairs("mem_compressed_bytes")], "#9a6700"))
    charts.append(("Memory", _svg_chart(mem_series, ylabel="GiB", bands=bands)))

    if pairs("cpu_speed_limit"):
        charts.append(("Thermal speed limit", _svg_chart(
            [("CPU speed limit %", pairs("cpu_speed_limit"), "#9a6700")],
            ylabel="%", ymax=100, bands=bands)))
    if pairs("gpu_util_pct"):
        charts.append(("GPU utilisation", _svg_chart(
            [("GPU busy %", pairs("gpu_util_pct"), "#1a7f37")],
            ylabel="%", ymax=100, bands=bands)))
    if pairs("package_power_w"):
        charts.append(("Package power", _svg_chart(
            [("CPU+GPU+ANE (W)", pairs("package_power_w"), "#8250df")], ylabel="W", bands=bands)))

    sim = [r for r in run.get("workload", {}).get("sim_reports", []) if r.get("fps") is not None]
    if sim:
        t0 = sim[0]["ts"]
        fps_pts = [((r["ts"] - t0) / 1000.0, r["fps"]) for r in sim]
        charts.append(("Web 3D simulator frame rate", _svg_chart(
            [("FPS", fps_pts, "#1f883d")], ylabel="FPS")))

    icon = {OK: "ok", WARN: "warn", FAIL: "fail", INFO: "info"}
    name = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", INFO: "NOTE"}

    md_rows = to_markdown(run, a, overall, findings, recs)
    table_md = md_rows.split("## Headline numbers\n")[1].split("\n\n")[0].strip().splitlines()
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(c.strip())}</td>"
                         for c in row.strip().strip("|").split("|")) + "</tr>"
        for row in table_md[2:])

    parts = [f"<!doctype html><html><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Laptop benchmark - {html.escape(si.get('model') or si.get('hostname') or '')}</title>",
             f"<style>{CSS}</style></head><body><div class='wrap'>"]
    parts.append("<h1>Laptop benchmark: CAD + web 3D + browser tabs</h1>")
    parts.append(f"<p class='lead'>{html.escape(si.get('model') or '')} &middot; "
                 f"{html.escape(str(si.get('cpu')))} &middot; run {run['started_at']} &middot; "
                 f"profile <code>{run['config']['profile']}</code></p>")
    parts.append(f"<div class='verdict'><span class='badge {icon[overall]}'>{label}</span>"
                 f"<span>{html.escape(blurb)}</span></div>")

    parts.append("<h2>Findings</h2>")
    for sev, title, detail in findings:
        parts.append(f"<div class='finding'><span class='tag {icon[sev]}'>{name[sev]}</span>"
                     f"<span><b>{html.escape(title)}</b>"
                     f"<span class='muted'>{html.escape(detail)}</span></span></div>")

    parts.append("<h2>Recommendations</h2><ul>")
    for r in recs:
        parts.append(f"<li>{html.escape(r)}</li>")
    parts.append("</ul>")

    parts.append("<h2>Headline numbers</h2><table><thead><tr><th>Metric</th><th>Value</th>"
                 f"</tr></thead><tbody>{body_rows}</tbody></table>")

    parts.append("<h2>Timeline</h2>")
    for title, svg in charts:
        parts.append(f"<div class='chart'><h3>{html.escape(title)}</h3>{svg}</div>")

    parts.append("<h2>Machine</h2><pre>" + html.escape("\n".join(summary_lines(si))) + "</pre>")
    parts.append(f"<p class='muted'>Generated by lsbench {run.get('tool_version', '')} &middot; "
                 f"{len(samples)} telemetry samples &middot; raw data in report.json</p>")
    parts.append("</div></body></html>")
    return "".join(parts)
