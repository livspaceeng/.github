"""Local asset server + Chrome launcher for the browser half of the workload.

The pages POST their own telemetry (FPS, frame times, JS heap) back to
`/report`, which is how we measure what the browser actually achieved rather
than guessing from CPU counters.

Chrome always runs against a throwaway --user-data-dir, so the operator's real
profile, session, extensions and cookies are never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .util import IS_LINUX, IS_MAC

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webassets")

MAC_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]
LINUX_BROWSERS = [
    "/opt/pw-browsers/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/usr/bin/chromium-browser", "/snap/bin/chromium",
]


def find_browser(explicit=None):
    """Return a path to a Chromium-family browser, or None."""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    env = os.environ.get("LSB_BROWSER") or os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for p in (MAC_BROWSERS if IS_MAC else LINUX_BROWSERS):
        if os.path.exists(p):
            return p
    if IS_LINUX:
        # Playwright installs under a versioned directory.
        import glob
        for pat in ("/opt/pw-browsers/*/chrome-linux/chrome", "/opt/pw-browsers/chromium*/chrome-linux/chrome"):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


class _Handler(SimpleHTTPRequestHandler):
    reports = None      # set on the class by ReportServer
    lock = threading.Lock()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ASSETS, **kw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        with self.lock:
            self.reports.append(payload)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_a):
        pass            # keep the console clean


class ReportServer:
    def __init__(self, port=0):
        self.reports = []
        _Handler.reports = self.reports
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def of_kind(self, kind):
        return [r for r in self.reports if r.get("kind") == kind]


class Browser:
    """One or more Chrome processes, each in a disposable profile.

    Headed Chrome takes every URL as a positional argument and opens them as
    tabs in a single window - that is what a real desktop session looks like,
    and it is the path taken on macOS.

    Headless Chrome refuses more than one target per process ("Multiple targets
    are not supported in headless mode"), so in headless mode each URL gets its
    own process and profile. That is only used for CI self-tests, where process
    topology does not matter.
    """

    def __init__(self, binary, urls, headless=False, keep_tabs_active=False, extra_flags=()):
        self.binary = binary
        self.urls = list(urls)
        self.headless = headless
        self.keep_tabs_active = keep_tabs_active
        self.extra_flags = list(extra_flags)
        self.procs = []
        self.profiles = []

    def flags(self, profile):
        f = [
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
            "--password-store=basic",
            "--test-type",
            "--window-size=1600,1000",
        ]
        if self.keep_tabs_active:
            # Off by default: real background tabs *are* throttled, and pretending
            # otherwise would overstate the load.
            f += [
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        if self.headless:
            f += ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                  "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        elif IS_LINUX and hasattr(os, "geteuid") and os.geteuid() == 0:
            f += ["--no-sandbox"]
        return f + self.extra_flags

    def _spawn(self, urls):
        profile = tempfile.mkdtemp(prefix="lsb-chrome-")
        self.profiles.append(profile)
        proc = subprocess.Popen(
            [self.binary] + self.flags(profile) + list(urls),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.procs.append(proc)
        return proc

    def start(self):
        if self.headless:
            for url in self.urls:
                self._spawn([url])
        else:
            self._spawn(self.urls)
        return self

    @property
    def primary(self):
        """The process rendering the 3D simulator (always the first URL)."""
        return self.procs[0] if self.procs else None

    @property
    def pid(self):
        return self.primary.pid if self.primary else None

    @property
    def profile(self):
        return self.profiles[0] if self.profiles else None

    def alive(self):
        return bool(self.primary) and self.primary.poll() is None

    def dead_count(self):
        return sum(1 for p in self.procs if p.poll() is not None)

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
        for profile in self.profiles:
            shutil.rmtree(profile, ignore_errors=True)
