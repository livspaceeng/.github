"""Orchestrates the concurrent workload: CAD + web 3D simulator + browser tabs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from .browser import Browser, ReportServer, find_browser

# A realistic office tab mix, with per-tab retained heap in MiB. These are in
# the range real Google apps occupy after an hour of use.
DEFAULT_TABS = [
    ("Gmail", 260),
    ("Google Docs", 210),
    ("Google Sheets", 300),
    ("Google Drive", 180),
    ("Google Calendar", 150),
    ("Google Meet", 340),
    ("Slack", 280),
    ("YouTube", 220),
]


def default_working_set_mb(ram_bytes):
    """Size the in-memory CAD assembly relative to installed RAM."""
    if not ram_bytes:
        return 3072
    ram_gb = ram_bytes / (1024 ** 3)
    return int(min(9216, max(2048, ram_gb * 0.32 * 1024)))


class Workload:
    def __init__(self, cfg, binary, scratch, sampler=None, log=print):
        self.cfg = cfg
        self.binary = binary
        self.scratch = scratch
        self.sampler = sampler
        self.log = log
        self.server = None
        self.browser = None
        self.cad = None
        self.cad_events_path = os.path.join(scratch, "cad_events.jsonl")
        self.started_components = []
        self.problems = []

    # -- components ---------------------------------------------------

    def _start_cad(self, duration):
        cmd = [
            sys.executable, "-m", "lsb.cadproc",
            "--events", self.cad_events_path,
            "--burn", self.binary,
            "--scratch", os.path.join(self.scratch, "cad"),
            "--duration", str(duration),
            "--working-set-mb", str(self.cfg["cad_working_set_mb"]),
            "--burst-every", str(self.cfg["cad_burst_every"]),
            "--burst-seconds", str(self.cfg["cad_burst_seconds"]),
            "--burst-threads", str(self.cfg["cad_burst_threads"]),
            "--autosave-every", str(self.cfg["cad_autosave_every"]),
            "--autosave-mb", str(self.cfg["cad_autosave_mb"]),
        ]
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        self.cad = subprocess.Popen(cmd, cwd=repo_root, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.started_components.append("cad")
        self.log(f"  CAD emulator      : pid {self.cad.pid}, "
                 f"{self.cfg['cad_working_set_mb']} MiB assembly, "
                 f"{self.cfg['cad_burst_threads']}-thread rebuilds every "
                 f"{self.cfg['cad_burst_every']:.0f}s")

    def _start_browser(self):
        binary = find_browser(self.cfg.get("browser_path"))
        if not binary:
            self.problems.append(
                "No Chromium-family browser found - the 3D simulator and tab load were "
                "skipped. Install Google Chrome or pass --browser /path/to/browser."
            )
            self.log("  Browser           : NOT FOUND - web workload skipped")
            return

        self.server = ReportServer().start()
        base = self.server.base
        sim = (f"{base}/sim.html?objects={self.cfg['sim_objects']}"
               f"&seg={self.cfg['sim_segments']}&res={self.cfg['sim_res']}")
        urls = [sim]
        for name, mb in DEFAULT_TABS[: self.cfg["tabs"]]:
            urls.append(f"{base}/tab.html?name={name.replace(' ', '+')}&mb={mb}")

        self.browser = Browser(
            binary, urls,
            headless=self.cfg["headless"],
            keep_tabs_active=self.cfg["keep_tabs_active"],
            extra_flags=self.cfg.get("browser_flags") or (),
        ).start()
        self.started_components.append("browser")
        tab_mb = sum(mb for _, mb in DEFAULT_TABS[: self.cfg["tabs"]])
        self.log(f"  Browser           : {os.path.basename(binary)} pid {self.browser.pid}, "
                 f"1 WebGL simulator + {self.cfg['tabs']} tabs (~{tab_mb} MiB tab heap)")
        self.log(f"  Profile           : throwaway at {self.browser.profile}")

    # -- run ----------------------------------------------------------

    def run(self, duration, on_tick=None, tick_every=15.0):
        self.log("Starting workload components:")
        self._start_cad(duration)
        self._start_browser()

        t0 = time.monotonic()
        end = t0 + duration
        next_tick = t0 + tick_every
        while time.monotonic() < end:
            time.sleep(0.5)
            now = time.monotonic()
            if self.cad and self.cad.poll() is not None and now < end - 2:
                err = (self.cad.stderr.read() or "").strip()[:300] if self.cad.stderr else ""
                self.problems.append(f"CAD emulator exited early (rc={self.cad.returncode}) {err}")
                self.cad = None
            if self.browser and not self.browser.alive() and now < end - 2:
                self.problems.append("Browser exited early; web workload metrics are incomplete.")
                self.browser = None
            if on_tick and now >= next_tick:
                on_tick(now - t0, duration)
                next_tick = now + tick_every
        return self.collect()

    def collect(self):
        data = {"problems": self.problems, "components": self.started_components}
        events = []
        if os.path.exists(self.cad_events_path):
            with open(self.cad_events_path) as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        data["cad_events"] = events
        rebuilds = [e["result"]["gflops"] for e in events
                    if e.get("event") == "rebuild_done" and (e.get("result") or {}).get("gflops")]
        saves = [e for e in events if e.get("event") == "autosave"]
        data["cad"] = {
            "rebuilds": len(rebuilds),
            "rebuild_gflops": rebuilds,
            "autosaves": len(saves),
            "autosave_write_mb_s": [s.get("write_mb_s") for s in saves],
            "working_set_mib": next((e.get("mib") for e in events
                                     if e.get("event") == "working_set_ready"), None),
            "alloc_mib_per_s": next((e.get("alloc_mib_per_s") for e in events
                                     if e.get("event") == "working_set_ready"), None),
        }
        if self.server:
            data["sim_reports"] = self.server.of_kind("sim")
            data["tab_reports"] = self.server.of_kind("tab")
        else:
            data["sim_reports"] = []
            data["tab_reports"] = []
        return data

    def stop(self):
        if self.cad and self.cad.poll() is None:
            self.cad.terminate()
            try:
                self.cad.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.cad.kill()
        if self.browser:
            self.browser.stop()
        if self.server:
            self.server.stop()
