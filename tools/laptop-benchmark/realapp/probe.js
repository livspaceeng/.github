/*
 * lsprobe - measures a real WebGL web application in the browser you are
 * already using, inside your own logged-in session.
 *
 * Built for Livspace Parametric / Canvas, but it is app-agnostic: it works on
 * Coohom, or any WebGL page.
 *
 * Usage: open the app, log in, open DevTools (Option-Cmd-I) -> Console,
 * paste this whole file, press Enter. Then USE THE APP normally.
 *
 *   __lsprobe.mark("orbiting")     label whatever you are doing
 *   __lsprobe.stop()               finish early and download the report
 *
 * It stops automatically after the configured duration and downloads an HTML
 * report plus the raw JSON.
 *
 * It only reads timing counters. It sends nothing anywhere, and it does not
 * touch app state, storage or credentials.
 */
(function () {
  "use strict";

  if (window.__lsprobe && window.__lsprobe.running) {
    console.warn("[lsprobe] already running - call __lsprobe.stop() first");
    return;
  }

  var CONFIG = {
    durationSeconds: (window.__LSPROBE_SECONDS | 0) || 180,
    bucketMs: 1000,          // one aggregated row per second
    jankMs: 33.34,           // below 30 fps
    badMs: 50,               // visibly dropped
    stallMs: 250,            // "it froze"
  };

  /* ---------------------------------------------------------- state */
  var S = {
    t0: performance.now(),
    frames: 0,
    frameTimes: [],          // every frame delta, whole session
    buckets: [],             // per-second aggregates
    marks: [],
    longTasks: [],
    drawCalls: 0,
    triangles: 0,
    bucketDraws: 0,
    bucketTris: 0,
    texBytes: 0,
    bufBytes: 0,
    contexts: 0,
    lastFrame: performance.now(),
    bucketStart: performance.now(),
    bucketTimes: [],
    running: true,
    raf: null,
    label: "idle",
  };

  /* ------------------------------------------------- WebGL hooking */
  // Patching the prototypes catches contexts that already exist, so this works
  // on an app that is already running.
  var TRI_MODES = {};        // filled once we see a context

  function hookContext(proto, name) {
    if (!proto || proto.__lsprobeHooked) return;
    proto.__lsprobeHooked = true;
    S.contexts++;

    function trisFor(mode, count, gl) {
      if (mode === gl.TRIANGLES) return count / 3;
      if (mode === gl.TRIANGLE_STRIP || mode === gl.TRIANGLE_FAN) return Math.max(0, count - 2);
      return 0;                            // points, lines - not triangles
    }

    var origElements = proto.drawElements;
    if (origElements) {
      proto.drawElements = function (mode, count) {
        S.drawCalls++; S.bucketDraws++;
        var t = trisFor(mode, count, this); S.triangles += t; S.bucketTris += t;
        return origElements.apply(this, arguments);
      };
    }
    var origArrays = proto.drawArrays;
    if (origArrays) {
      proto.drawArrays = function (mode, first, count) {
        S.drawCalls++; S.bucketDraws++;
        var t = trisFor(mode, count, this); S.triangles += t; S.bucketTris += t;
        return origArrays.apply(this, arguments);
      };
    }
    ["drawElementsInstanced", "drawArraysInstanced"].forEach(function (fn) {
      var orig = proto[fn];
      if (!orig) return;
      var isElements = fn.indexOf("Elements") >= 0;
      proto[fn] = function (mode, a, b, c) {
        var count = isElements ? a : b;
        var instances = isElements ? c : c;
        S.drawCalls++; S.bucketDraws++;
        var t = trisFor(mode, count, this) * (instances || 1);
        S.triangles += t; S.bucketTris += t;
        return orig.apply(this, arguments);
      };
    });

    // Rough GPU-resident bytes, to spot a memory-heavy scene.
    var origTex = proto.texImage2D;
    if (origTex) {
      proto.texImage2D = function () {
        try {
          var w = arguments[3], h = arguments[4];
          if (typeof w === "number" && typeof h === "number") S.texBytes += w * h * 4;
          else {
            var src = arguments[5];
            if (src && src.width && src.height) S.texBytes += src.width * src.height * 4;
          }
        } catch (e) { /* never break the app */ }
        return origTex.apply(this, arguments);
      };
    }
    var origBuf = proto.bufferData;
    if (origBuf) {
      proto.bufferData = function (target, data) {
        try {
          if (data && typeof data === "object" && data.byteLength) S.bufBytes += data.byteLength;
          else if (typeof data === "number") S.bufBytes += data;
        } catch (e) {}
        return origBuf.apply(this, arguments);
      };
    }
    console.log("[lsprobe] hooked " + name);
  }

  hookContext(window.WebGLRenderingContext && WebGLRenderingContext.prototype, "WebGL1");
  hookContext(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype, "WebGL2");

  /* ------------------------------------------------ renderer info */
  function gpuInfo() {
    var info = { renderer: null, vendor: null, maxTexture: null, canvases: [] };
    var canvases = document.querySelectorAll("canvas");
    for (var i = 0; i < canvases.length; i++) {
      var c = canvases[i];
      var gl = null;
      try { gl = c.getContext("webgl2") || c.getContext("webgl"); } catch (e) {}
      info.canvases.push({
        w: c.width, h: c.height,
        cssW: Math.round(c.clientWidth), cssH: Math.round(c.clientHeight),
        webgl: !!gl,
      });
      if (gl && !info.renderer) {
        var dbg = gl.getExtension("WEBGL_debug_renderer_info");
        if (dbg) {
          info.renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
          info.vendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
        }
        info.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        info.version = gl.getParameter(gl.VERSION);
      }
    }
    return info;
  }

  /* -------------------------------------------------- long tasks */
  // A long task is the main thread blocked - the actual cause of "it feels
  // laggy" and of input that does not respond.
  try {
    var po = new PerformanceObserver(function (list) {
      list.getEntries().forEach(function (e) {
        S.longTasks.push({ t: +(e.startTime - S.t0).toFixed(0), ms: +e.duration.toFixed(1) });
      });
    });
    po.observe({ entryTypes: ["longtask"] });
    S.longTaskSupported = true;
  } catch (e) {
    S.longTaskSupported = false;
  }

  /* ------------------------------------------------- frame timing */
  function tick(now) {
    if (!S.running) return;
    var dt = now - S.lastFrame;
    S.lastFrame = now;
    if (dt > 0 && dt < 5000) {
      S.frames++;
      S.frameTimes.push(dt);
      S.bucketTimes.push(dt);
    }

    if (now - S.bucketStart >= CONFIG.bucketMs) {
      var secs = (now - S.bucketStart) / 1000;
      var times = S.bucketTimes;
      var sorted = times.slice().sort(function (a, b) { return a - b; });
      var pick = function (q) {
        return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))] : 0;
      };
      var mem = performance.memory
        ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(1) : null;
      S.buckets.push({
        t: +((now - S.t0) / 1000).toFixed(1),
        label: S.label,
        fps: +(times.length / secs).toFixed(2),
        avg: +(times.reduce(function (a, b) { return a + b; }, 0) / (times.length || 1)).toFixed(2),
        p95: +pick(0.95).toFixed(2),
        max: +pick(1).toFixed(2),
        jank: times.filter(function (x) { return x > CONFIG.jankMs; }).length,
        draws: S.bucketDraws,
        tris: S.bucketTris,
        heapMb: mem,
      });
      S.bucketTimes = []; S.bucketDraws = 0; S.bucketTris = 0;
      S.bucketStart = now;
    }

    if ((now - S.t0) / 1000 >= CONFIG.durationSeconds) { api.stop(); return; }
    S.raf = requestAnimationFrame(tick);
  }

  /* ------------------------------------------------------ report */
  function pct(arr, q) {
    if (!arr.length) return null;
    var s = arr.slice().sort(function (a, b) { return a - b; });
    var pos = (s.length - 1) * (q / 100), lo = Math.floor(pos), hi = Math.min(lo + 1, s.length - 1);
    return s[lo] + (s[hi] - s[lo]) * (pos - lo);
  }

  function summarise() {
    var ft = S.frameTimes;
    var elapsed = (performance.now() - S.t0) / 1000;
    var fpsAll = S.buckets.map(function (b) { return b.fps; });
    var toMs = function (v) { return v == null ? null : +v.toFixed(2); };
    var summary = {
      seconds: +elapsed.toFixed(1),
      frames: S.frames,
      fps_mean: +(S.frames / elapsed).toFixed(2),
      fps_p50: fpsAll.length ? +pct(fpsAll, 50).toFixed(2) : null,
      fps_p05: fpsAll.length ? +pct(fpsAll, 5).toFixed(2) : null,
      fps_min: fpsAll.length ? +Math.min.apply(null, fpsAll).toFixed(2) : null,
      frame_ms_p50: toMs(pct(ft, 50)),
      frame_ms_p95: toMs(pct(ft, 95)),
      frame_ms_p99: toMs(pct(ft, 99)),
      frame_ms_max: ft.length ? +Math.max.apply(null, ft).toFixed(2) : null,
      jank_frames: ft.filter(function (x) { return x > CONFIG.jankMs; }).length,
      bad_frames: ft.filter(function (x) { return x > CONFIG.badMs; }).length,
      stalls: ft.filter(function (x) { return x > CONFIG.stallMs; }).length,
      draw_calls_total: S.drawCalls,
      draw_calls_per_frame: S.frames ? +(S.drawCalls / S.frames).toFixed(1) : 0,
      triangles_per_frame: S.frames ? Math.round(S.triangles / S.frames) : 0,
      texture_mb_uploaded: +(S.texBytes / 1048576).toFixed(1),
      buffer_mb_uploaded: +(S.bufBytes / 1048576).toFixed(1),
      long_tasks: S.longTasks.length,
      long_task_ms_total: +S.longTasks.reduce(function (a, b) { return a + b.ms; }, 0).toFixed(0),
      long_task_ms_max: S.longTasks.length
        ? Math.max.apply(null, S.longTasks.map(function (x) { return x.ms; })) : 0,
      long_task_supported: S.longTaskSupported,
      heap_mb_peak: Math.max.apply(null, [0].concat(
        S.buckets.map(function (b) { return b.heapMb || 0; }))) || null,
      device_pixel_ratio: window.devicePixelRatio,
      hardware_concurrency: navigator.hardwareConcurrency,
      device_memory_gb: navigator.deviceMemory || null,
      url: location.href.split("?")[0],
      user_agent: navigator.userAgent,
      gpu: gpuInfo(),
      config: CONFIG,
    };
    summary.jank_pct = S.frames ? +(100 * summary.jank_frames / S.frames).toFixed(1) : 0;
    summary.software_rendered = !!(summary.gpu.renderer &&
      /swiftshader|llvmpipe|software|angle \(google/i.test(summary.gpu.renderer));
    return summary;
  }

  function verdict(s) {
    var f = [], sev = { ok: 0, warn: 0, fail: 0 };
    function add(level, title, detail) { f.push({ level: level, title: title, detail: detail }); sev[level]++; }

    if (!s.frames) {
      add("fail", "No frames captured",
        "The page never rendered while the probe was running. Make sure the 3D view is open and visible.");
      return { findings: f, overall: "fail" };
    }
    if (s.software_rendered) {
      add("fail", "Not using the GPU",
        "The renderer reports as " + s.gpu.renderer + ". Chrome is falling back to software " +
        "rendering, which is why it is slow. Check chrome://gpu - hardware acceleration is " +
        "off or blocklisted. Fix this before judging the laptop.");
    }

    var fps = s.fps_p50;
    if (fps >= 50) add("ok", "Frame rate is smooth", "Median " + fps + " fps, 1% low " + s.fps_p05 + ".");
    else if (fps >= 30) add("warn", "Frame rate is usable but not smooth",
      "Median " + fps + " fps, 1% low " + s.fps_p05 + ". Orbiting will feel heavy.");
    else if (fps >= 20) add("warn", "Frame rate is poor",
      "Median " + fps + " fps, 1% low " + s.fps_p05 + ". Interaction will feel sluggish.");
    else add("fail", "Frame rate is unusable",
      "Median " + fps + " fps, 1% low " + s.fps_p05 + ".");

    if (s.frame_ms_p99 > 200) add("fail", "Severe frame stalls",
      "The worst 1% of frames took " + s.frame_ms_p99 + " ms (peak " + s.frame_ms_max +
      " ms). These are the freezes users complain about.");
    else if (s.frame_ms_p95 > 50) add("warn", "Inconsistent frame pacing",
      "95th-percentile frame time " + s.frame_ms_p95 + " ms. Motion will stutter even if the average looks fine.");
    else add("ok", "Frame pacing is consistent", "95th-percentile frame time " + s.frame_ms_p95 + " ms.");

    if (s.long_task_supported) {
      var blockedPct = 100 * s.long_task_ms_total / (s.seconds * 1000);
      if (blockedPct > 30) add("fail", "Main thread is saturated",
        "Blocked for " + blockedPct.toFixed(0) + "% of the session across " + s.long_tasks +
        " long tasks (worst " + s.long_task_ms_max + " ms). Clicks and typing will lag. " +
        "This is application JavaScript, not the GPU - a faster laptop helps less than you would expect.");
      else if (blockedPct > 10) add("warn", "Main thread often blocked",
        "Blocked for " + blockedPct.toFixed(0) + "% of the session, worst single task " +
        s.long_task_ms_max + " ms.");
      else add("ok", "Main thread stays responsive",
        "Blocked for only " + blockedPct.toFixed(0) + "% of the session.");
    }

    if (s.draw_calls_per_frame > 2000) add("warn", "Very high draw-call count",
      s.draw_calls_per_frame + " draw calls per frame. This is a CPU-side bottleneck in the app " +
      "(batching), not a GPU limit.");
    else if (s.draw_calls_per_frame > 0) add("ok", "Draw-call count is reasonable",
      s.draw_calls_per_frame + " draw calls per frame, " +
      (s.triangles_per_frame / 1e6).toFixed(2) + "M triangles.");

    if (s.heap_mb_peak && s.heap_mb_peak > 2000) add("warn", "Very large JS heap",
      "Peak " + s.heap_mb_peak + " MB. Close to Chrome's per-tab ceiling; a bigger design may crash the tab.");
    else if (s.heap_mb_peak) add("ok", "JS heap is within budget", "Peak " + s.heap_mb_peak + " MB.");

    if (s.device_pixel_ratio >= 2 && s.gpu.canvases.length) {
      var c = s.gpu.canvases.filter(function (x) { return x.webgl; })[0];
      if (c && c.w >= 3000) add("warn", "Rendering at full Retina resolution",
        "The 3D canvas is " + c.w + "x" + c.h + " backing pixels. Halving the render scale is " +
        "usually the single biggest win available, at little visual cost.");
    }

    var order = { fail: 0, warn: 1, ok: 2 };
    f.sort(function (a, b) { return order[a.level] - order[b.level]; });
    return { findings: f, overall: sev.fail ? "fail" : (sev.warn ? "warn" : "ok") };
  }

  /* ------------------------------------------------ HTML rendering */
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function chart(buckets, key, label, colour, ymaxHint) {
    if (!buckets.length) return "<p>no data</p>";
    var W = 860, H = 180, PL = 48, PR = 12, PT = 12, PB = 26;
    var xs = buckets.map(function (b) { return b.t; });
    var ys = buckets.map(function (b) { return b[key] || 0; });
    var xmax = Math.max.apply(null, xs) || 1;
    var ymax = Math.max(ymaxHint || 0, Math.max.apply(null, ys)) * 1.1 || 1;
    var X = function (v) { return PL + (W - PL - PR) * (v / xmax); };
    var Y = function (v) { return PT + (H - PT - PB) * (1 - v / ymax); };
    var d = buckets.map(function (b, i) {
      return (i ? "L" : "M") + X(b.t).toFixed(1) + "," + Y(b[key] || 0).toFixed(1); }).join(" ");
    var out = ['<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%">'];
    for (var i = 0; i <= 4; i++) {
      var y = PT + (H - PT - PB) * i / 4;
      out.push('<line x1="' + PL + '" y1="' + y + '" x2="' + (W - PR) + '" y2="' + y + '" class="g"/>');
      out.push('<text x="' + (PL - 6) + '" y="' + (y + 4) + '" class="tk" text-anchor="end">' +
        (ymax * (1 - i / 4)).toFixed(ymax < 10 ? 1 : 0) + "</text>");
    }
    if (key === "fps" && ymax > 30)
      out.push('<line x1="' + PL + '" y1="' + Y(30) + '" x2="' + (W - PR) + '" y2="' + Y(30) +
        '" stroke="#cf222e" stroke-dasharray="4 3" stroke-width="1"/>');
    out.push('<path d="' + d + '" fill="none" stroke="' + colour + '" stroke-width="1.8"/>');
    for (var j = 0; j <= 4; j++) {
      var x = PL + (W - PL - PR) * j / 4;
      out.push('<text x="' + x + '" y="' + (H - 6) + '" class="tk" text-anchor="middle">' +
        (xmax * j / 4).toFixed(0) + "s</text>");
    }
    out.push('<text x="4" y="' + (PT + 8) + '" class="tk">' + esc(label) + "</text></svg>");
    return out.join("");
  }

  function buildHtml(s, v) {
    var badge = { ok: ["PASS", "#1a7f37"], warn: ["MARGINAL", "#9a6700"], fail: ["FAIL", "#cf222e"] }[v.overall];
    var rows = [
      ["Median frame rate", s.fps_p50 + " fps"],
      ["1% low frame rate", s.fps_p05 + " fps"],
      ["Frame time p50 / p95 / p99", s.frame_ms_p50 + " / " + s.frame_ms_p95 + " / " + s.frame_ms_p99 + " ms"],
      ["Worst frame", s.frame_ms_max + " ms"],
      ["Janky frames (>33ms)", s.jank_frames + " (" + s.jank_pct + "%)"],
      ["Freezes (>250ms)", String(s.stalls)],
      ["Main-thread long tasks", s.long_task_supported
        ? s.long_tasks + ", " + s.long_task_ms_total + " ms total, worst " + s.long_task_ms_max + " ms"
        : "not supported in this browser"],
      ["Draw calls per frame", String(s.draw_calls_per_frame)],
      ["Triangles per frame", (s.triangles_per_frame / 1e6).toFixed(2) + "M"],
      ["Texture data uploaded", s.texture_mb_uploaded + " MB"],
      ["Geometry data uploaded", s.buffer_mb_uploaded + " MB"],
      ["Peak JS heap", s.heap_mb_peak ? s.heap_mb_peak + " MB" : "n/a"],
      ["GPU", (s.gpu.renderer || "unknown")],
      ["Canvas backing size", s.gpu.canvases.filter(function (c) { return c.webgl; })
        .map(function (c) { return c.w + "x" + c.h; }).join(", ") || "n/a"],
      ["Device pixel ratio", String(s.device_pixel_ratio)],
      ["CPU threads reported", String(s.hardware_concurrency)],
      ["Session length", s.seconds + " s, " + s.frames + " frames"],
    ];
    var labels = {};
    s.__buckets.forEach(function (b) { labels[b.label] = (labels[b.label] || 0) + 1; });

    return "<!doctype html><meta charset='utf-8'><title>Real-app performance - " +
      esc(s.url) + "</title><style>" +
      "body{margin:0;background:#fff;color:#1f2328;font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}" +
      "@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}" +
      ".card{background:#151b23!important;border-color:#3d444d!important}td,th{border-color:#3d444d!important}}" +
      ".w{max-width:940px;margin:0 auto;padding:36px 22px 70px}h1{font-size:24px;margin:0 0 4px}" +
      "h2{font-size:17px;margin:32px 0 10px;border-bottom:1px solid #d1d9e0;padding-bottom:6px}" +
      ".card{background:#f6f8fa;border:1px solid #d1d9e0;border-radius:10px;padding:14px;margin:12px 0}" +
      ".bg{display:inline-block;color:#fff;font-weight:700;padding:6px 14px;border-radius:999px}" +
      "table{border-collapse:collapse;width:100%;font-size:14px}" +
      "td,th{text-align:left;padding:7px 10px;border-bottom:1px solid #d1d9e0}" +
      "td:last-child{font-variant-numeric:tabular-nums}" +
      ".f{display:flex;gap:11px;padding:11px 0;border-bottom:1px solid #d1d9e0}" +
      ".t{font-size:11px;font-weight:700;color:#fff;padding:3px 8px;border-radius:5px;height:fit-content;min-width:50px;text-align:center}" +
      ".g{stroke:#d1d9e0}.tk{fill:#59636e;font-size:10px}small{color:#59636e}</style>" +
      "<div class='w'><h1>Real-app performance report</h1>" +
      "<p><small>" + esc(s.url) + "<br>" + esc(s.user_agent) + "<br>" + new Date().toString() + "</small></p>" +
      "<p><span class='bg' style='background:" + badge[1] + "'>" + badge[0] + "</span></p>" +
      "<h2>Findings</h2>" + v.findings.map(function (f) {
        var c = { ok: "#1a7f37", warn: "#9a6700", fail: "#cf222e" }[f.level];
        var n = { ok: "PASS", warn: "WARN", fail: "FAIL" }[f.level];
        return "<div class='f'><span class='t' style='background:" + c + "'>" + n +
          "</span><span><b>" + esc(f.title) + "</b><br><small>" + esc(f.detail) + "</small></span></div>";
      }).join("") +
      "<h2>Measurements</h2><table>" + rows.map(function (r) {
        return "<tr><td>" + esc(r[0]) + "</td><td>" + esc(r[1]) + "</td></tr>"; }).join("") + "</table>" +
      "<h2>Frame rate over time</h2><div class='card'>" +
      chart(s.__buckets, "fps", "fps", "#1f883d", 60) + "</div>" +
      "<h2>Frame time p95</h2><div class='card'>" +
      chart(s.__buckets, "p95", "ms", "#bf3989") + "</div>" +
      "<h2>Draw calls per second</h2><div class='card'>" +
      chart(s.__buckets, "draws", "calls", "#0969da") + "</div>" +
      (s.heap_mb_peak ? "<h2>JS heap</h2><div class='card'>" +
        chart(s.__buckets, "heapMb", "MB", "#8250df") + "</div>" : "") +
      "<h2>Activity labels</h2><p>" + esc(JSON.stringify(labels)) + "</p>" +
      "<p><small>Generated by lsprobe. Raw JSON downloaded alongside this file.</small></p></div>";
  }

  function download(name, text, type) {
    try {
      var blob = new Blob([text], { type: type });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
      return true;
    } catch (e) {
      console.error("[lsprobe] download failed", e);
      return false;
    }
  }

  /* --------------------------------------------------------- api */
  var api = {
    running: true,
    mark: function (label) {
      S.label = String(label || "idle");
      S.marks.push({ t: +((performance.now() - S.t0) / 1000).toFixed(1), label: S.label });
      console.log("[lsprobe] mark: " + S.label);
    },
    status: function () {
      var s = summarise();
      console.log("[lsprobe] " + s.seconds + "s  " + s.fps_p50 + " fps median  p95 " +
        s.frame_ms_p95 + "ms  " + s.draw_calls_per_frame + " draws/frame");
      return s;
    },
    stop: function () {
      if (!S.running) return;
      S.running = false; api.running = false;
      if (S.raf) cancelAnimationFrame(S.raf);
      var s = summarise();
      s.__buckets = S.buckets;
      s.marks = S.marks;
      var v = verdict(s);
      var stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      download("lsprobe-" + stamp + ".html", buildHtml(s, v), "text/html");
      download("lsprobe-" + stamp + ".json",
        JSON.stringify({ summary: s, verdict: v, buckets: S.buckets,
                         longTasks: S.longTasks, marks: S.marks }, null, 1), "application/json");
      console.log("%c[lsprobe] " + v.overall.toUpperCase(), "font-weight:bold;font-size:14px");
      console.table(v.findings.map(function (f) {
        return { level: f.level, finding: f.title }; }));
      console.log("[lsprobe] report downloaded. Full data:", { summary: s, verdict: v });
      window.__lsprobeResult = { summary: s, verdict: v, buckets: S.buckets };
      return s;
    },
  };
  window.__lsprobe = api;

  S.raf = requestAnimationFrame(tick);
  console.log("%c[lsprobe] recording for " + CONFIG.durationSeconds + "s - USE THE APP NOW",
    "font-weight:bold;font-size:14px;color:#1f883d");
  console.log("[lsprobe] __lsprobe.mark('orbiting') to label activity, __lsprobe.stop() to finish early");
})();
