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
    hidden: [],              // [{from, to}] seconds the tab was in the background
    hiddenSince: null,
  };

  // Chrome stops requestAnimationFrame for a hidden tab. Without this the
  // resulting gap is indistinguishable from the page hanging, and gets counted
  // as a freeze it never had.
  (function trackVisibility() {
    function now() { return (performance.now() - S.t0) / 1000; }
    if (document.visibilityState === "hidden") S.hiddenSince = 0;
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        S.hiddenSince = now();
      } else if (S.hiddenSince != null) {
        S.hidden.push({ from: +S.hiddenSince.toFixed(1), to: +now().toFixed(1) });
        S.hiddenSince = null;
      }
    });
  })();

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

  function median(a) { return a.length ? pct(a, 50) : null; }

  // Seconds where the 3D view actually drew, versus seconds where the page was
  // only compositing. Averaging across both is what makes a stuttering app look
  // healthy, so every per-frame figure below is computed on the active seconds.
  function split() {
    var active = S.buckets.filter(function (b) { return b.draws > 0 && b.fps > 0.5; });
    var idle = S.buckets.filter(function (b) { return b.draws === 0; });
    return { active: active, idle: idle };
  }

  // A gap in the per-second timeline means no frame was produced. That is only
  // a hang if the tab was actually in front of the user AND something was
  // blocking the main thread - a backgrounded tab produces an identical gap.
  // Every gap is therefore classified against two independent signals:
  // the visibility log, and whether a Long Task actually covers it.
  function classifyGaps() {
    var hangs = [], hiddenGaps = [], b = S.buckets;
    if (S.hiddenSince != null) {
      S.hidden.push({ from: S.hiddenSince, to: (performance.now() - S.t0) / 1000 });
      S.hiddenSince = null;
    }
    for (var i = 1; i < b.length; i++) {
      var from = b[i - 1].t, to = b[i].t, gap = to - from;
      if (gap <= 2.5) continue;
      var rec = { from: +from.toFixed(1), to: +to.toFixed(1), seconds: +gap.toFixed(1) };

      var hiddenOverlap = 0;
      S.hidden.forEach(function (h) {
        hiddenOverlap += Math.max(0, Math.min(to, h.to) - Math.max(from, h.from));
      });
      var blockedMs = 0;
      S.longTasks.forEach(function (t) {
        var a = t.t / 1000, z = (t.t + t.ms) / 1000;
        blockedMs += Math.max(0, Math.min(to, z) - Math.max(from, a)) * 1000;
      });
      rec.blocked_ms = Math.round(blockedMs);

      // Visibility decides whether the gap counts at all - it is authoritative.
      // Long Tasks only attribute the cause, because a gap with no blocking
      // task is still a real stall to the user; it just came from the renderer
      // rather than from JavaScript.
      if (hiddenOverlap > gap * 0.5) {
        hiddenGaps.push(rec);
      } else {
        rec.cause = blockedMs >= gap * 1000 * 0.5 ? "main thread blocked" : "renderer stalled";
        hangs.push(rec);
      }
    }
    return { hangs: hangs, hidden: hiddenGaps };
  }

  function summarise() {
    var ft = S.frameTimes;
    var elapsed = (performance.now() - S.t0) / 1000;
    var fpsAll = S.buckets.map(function (b) { return b.fps; });
    var toMs = function (v) { return v == null ? null : +v.toFixed(2); };
    var sp = split(), gaps = classifyGaps();
    var fz = gaps.hangs;
    var frozenTotal = fz.reduce(function (a, f) { return a + f.seconds; }, 0);
    var hiddenTotal = gaps.hidden.reduce(function (a, f) { return a + f.seconds; }, 0);
    // Time the page was actually in front of the user. Every ratio below uses
    // this, not wall clock, or a long background spell flatters everything.
    var foreground = Math.max(1, elapsed - hiddenTotal);

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

      // The figures that matter, measured only while the view was rendering.
      active_seconds: sp.active.length,
      idle_seconds: sp.idle.length,
      frozen_seconds: +frozenTotal.toFixed(1),
      freezes: fz,
      hidden_seconds: +hiddenTotal.toFixed(1),
      hidden_gaps: gaps.hidden,
      foreground_seconds: +foreground.toFixed(1),
      worst_freeze_s: fz.length ? Math.max.apply(null, fz.map(function (f) { return f.seconds; })) : 0,
      active_fps_p50: sp.active.length ? +median(sp.active.map(function (b) { return b.fps; })).toFixed(1) : null,
      idle_fps_p50: sp.idle.length ? +median(sp.idle.map(function (b) { return b.fps; })).toFixed(1) : null,
      active_draws_per_frame: sp.active.length
        ? Math.round(median(sp.active.map(function (b) { return b.draws / b.fps; }))) : 0,
      active_draws_per_frame_max: sp.active.length
        ? Math.round(Math.max.apply(null, sp.active.map(function (b) { return b.draws / b.fps; }))) : 0,
      active_tris_per_frame: sp.active.length
        ? Math.round(median(sp.active.map(function (b) { return b.tris / b.fps; }))) : 0,
      active_tris_per_frame_max: sp.active.length
        ? Math.round(Math.max.apply(null, sp.active.map(function (b) { return b.tris / b.fps; }))) : 0,

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
      heap_mb_start: S.buckets.length ? S.buckets[0].heapMb : null,
      heap_mb_peak: Math.max.apply(null, [0].concat(
        S.buckets.map(function (b) { return b.heapMb || 0; }))) || null,
      heap_mb_end: S.buckets.length ? S.buckets[S.buckets.length - 1].heapMb : null,
      device_pixel_ratio: window.devicePixelRatio,
      hardware_concurrency: navigator.hardwareConcurrency,
      device_memory_gb: navigator.deviceMemory || null,
      url: location.href.split("?")[0],
      user_agent: navigator.userAgent,
      gpu: gpuInfo(),
      config: CONFIG,
    };
    summary.jank_pct = S.frames ? +(100 * summary.jank_frames / S.frames).toFixed(1) : 0;
    summary.blocked_pct = +(100 * summary.long_task_ms_total / (foreground * 1000)).toFixed(0);
    // Submission cost at 2-5 microseconds per WebGL draw call, CPU side.
    summary.submit_ms_low = +(summary.active_draws_per_frame * 0.002).toFixed(1);
    summary.submit_ms_high = +(summary.active_draws_per_frame * 0.005).toFixed(1);
    summary.frame_budget_ms = 16.7;
    summary.software_rendered = !!(summary.gpu.renderer &&
      /swiftshader|llvmpipe|software|basic render/i.test(summary.gpu.renderer));
    return summary;
  }

  function verdict(s) {
    var f = [];
    function add(level, title, detail) { f.push({ level: level, title: title, detail: detail }); }

    if (!s.frames) {
      add("fail", "No frames captured",
        "The page never rendered while the probe was running. Make sure the 3D view is open and visible.");
      return { findings: f, overall: "fail" };
    }
    if (s.software_rendered) {
      add("fail", "Not using the GPU",
        "The renderer reports as " + s.gpu.renderer + ". Chrome is falling back to software " +
        "rendering. Check chrome://gpu and fix hardware acceleration before judging anything else here.");
    }

    if (s.frozen_seconds > 5) {
      add("fail", "The page stopped responding",
        s.frozen_seconds + "s of the " + s.foreground_seconds + "s the page was actually on screen " +
        "produced no frames at all, across " +
        s.freezes.length + " episodes, the longest " + s.worst_freeze_s + "s" +
        (s.freezes.length ? " (" + s.freezes.filter(function (f) {
          return f.cause === "main thread blocked"; }).length + " of them main-thread blocking)" : "") +
        ". These are complete interface freezes and they are invisible in any average frame rate." +
        (s.hidden_seconds > 2 ? " A further " + s.hidden_seconds + "s where the tab was in the " +
        "background is excluded, not counted as a freeze." : ""));
    }

    var a = s.active_fps_p50, i = s.idle_fps_p50;
    if (a != null && i != null && i - a > 20) {
      add("warn", "Smooth until it has to draw",
        "Idle the page composites at " + i + " fps; while the 3D view redraws it manages " + a +
        " fps. The session median of " + s.fps_p50 + " fps is an artefact of counting the idle time.");
    } else if (a != null && a >= 50) {
      add("ok", "Frame rate holds while rendering", "Median " + a + " fps with the view live.");
    } else if (a != null) {
      add(a < 24 ? "fail" : "warn", "Frame rate is low while rendering", "Median " + a + " fps.");
    }

    if (s.submit_ms_low > s.frame_budget_ms) {
      add("fail", "Draw-call submission alone exceeds the frame budget",
        s.active_draws_per_frame.toLocaleString() + " draw calls per frame at roughly 2-5 microseconds " +
        "each is " + s.submit_ms_low + "-" + s.submit_ms_high + " ms of CPU work per frame, against a " +
        s.frame_budget_ms + " ms budget at 60 fps - before the GPU draws anything. This is a batching " +
        "problem in the application; faster hardware cannot close a gap this size.");
    } else if (s.active_draws_per_frame > 2000) {
      add("warn", "High draw-call count",
        s.active_draws_per_frame.toLocaleString() + " draw calls per frame. CPU-side, and worth batching.");
    } else if (s.active_draws_per_frame) {
      add("ok", "Draw-call count is reasonable", s.active_draws_per_frame.toLocaleString() + " per frame.");
    }

    var canvas = (s.gpu.canvases || []).filter(function (c) { return c.webgl && c.w > 100; })[0];
    if (s.active_tris_per_frame > 3000000 && canvas) {
      var px = canvas.w * canvas.h;
      add("warn", "Scene is drawn without effective culling",
        (s.active_tris_per_frame / 1e6).toFixed(1) + "M triangles per frame for a " + canvas.w + "x" +
        canvas.h + " buffer is about " + (s.active_tris_per_frame / px).toFixed(0) + " triangles per pixel. " +
        "Most of that geometry is off-screen or sub-pixel. Frustum culling and level-of-detail would cut it.");
    }

    if (s.long_task_supported) {
      if (s.blocked_pct > 30) add("fail", "Main thread is saturated",
        "Blocked for " + s.blocked_pct + "% of the session across " + s.long_tasks + " long tasks, " +
        "worst single task " + (s.long_task_ms_max / 1000).toFixed(1) + "s. While a long task runs " +
        "nothing happens - no frames, no clicks, no typing. This is application JavaScript.");
      else if (s.blocked_pct > 10) add("warn", "Main thread often blocked",
        "Blocked " + s.blocked_pct + "% of the session, worst task " +
        (s.long_task_ms_max / 1000).toFixed(1) + "s.");
      else add("ok", "Main thread stays responsive", "Blocked only " + s.blocked_pct + "% of the session.");
    }

    if (s.heap_mb_start && s.heap_mb_peak && s.heap_mb_peak - s.heap_mb_start > 400)
      add("warn", "JS heap grew sharply",
        Math.round(s.heap_mb_start) + " MB to a peak of " + Math.round(s.heap_mb_peak) + " MB in " +
        s.seconds + "s. Worth a longer recording to see whether it plateaus or keeps climbing.");

    if (s.device_pixel_ratio >= 2 && canvas && canvas.w >= 1800)
      add("warn", "Rendering at full Retina resolution",
        "The 3D canvas is " + canvas.w + "x" + canvas.h + " backing pixels. Halving the render scale is " +
        "usually the cheapest large win available.");

    var order = { fail: 0, warn: 1, ok: 2 };
    f.sort(function (x, y) { return order[x.level] - order[y.level]; });
    return { findings: f,
             overall: f.some(function (x) { return x.level === "fail"; }) ? "fail"
                    : f.some(function (x) { return x.level === "warn"; }) ? "warn" : "ok" };
  }

  /* ---------- report rendering ---------- */

  function esc(t) {
    return String(t).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }

  // One cell per second, classified: idle / rendering / frozen. This single
  // strip communicates more than any average in the table below it.
  function timeline(s) {
    var N = Math.ceil(s.seconds), W = 900, L = 8, R = 8, TOP = 30, H = 46;
    var state = [], fps = [];
    for (var k = 0; k < N; k++) { state.push("f"); fps.push(0); }
    S.buckets.forEach(function (b) {
      var t = Math.floor(b.t);
      if (t >= 0 && t < N) { state[t] = b.draws > 0 ? "r" : "s"; fps[t] = Math.round(b.fps); }
    });
    var cw = (W - L - R) / N;
    var fill = { s: "#0B7D63", r: "#A87400", f: "url(#hz)" };
    var out = ['<svg viewBox="0 0 ' + W + ' ' + (TOP + H + 26) + '" style="width:100%;height:auto" ' +
      'role="img" aria-label="Per-second state of the session">',
      '<defs><pattern id="hz" width="6" height="6" patternTransform="rotate(45)" ' +
      'patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#9E1F17"></rect>' +
      '<line x1="0" y1="0" x2="0" y2="6" stroke="#fff" stroke-width="2.2"></line></pattern></defs>'];
    for (var i = 0; i < N; i++) {
      out.push('<rect x="' + (L + i * cw).toFixed(2) + '" y="' + TOP + '" width="' +
        Math.max(0.8, cw - 0.6).toFixed(2) + '" height="' + H + '" fill="' + fill[state[i]] +
        '"><title>' + i + 's — ' + ({ s: "idle", r: "rendering", f: "frozen, no frames" })[state[i]] +
        (state[i] === "f" ? "" : " " + fps[i] + " fps") + '</title></rect>');
    }
    var step = N > 120 ? 30 : N > 60 ? 20 : 10;
    for (var t2 = 0; t2 <= N; t2 += step) {
      var x = L + t2 * cw;
      out.push('<text x="' + x.toFixed(1) + '" y="' + (TOP + H + 17) + '" fill="#59636e" ' +
        'font-family="ui-monospace,monospace" font-size="11" text-anchor="' +
        (t2 === 0 ? "start" : "middle") + '">' + t2 + 's</text>');
    }
    out.push("</svg>");
    var counts = { s: 0, r: 0, f: 0 };
    state.forEach(function (c) { counts[c]++; });
    out.push('<div class="lg">' +
      '<span><i style="background:#0B7D63"></i>Idle, compositing <b>' + counts.s + ' s</b></span>' +
      '<span><i style="background:#A87400"></i>Rendering the 3D view <b>' + counts.r + ' s</b></span>' +
      '<span><i style="background:#9E1F17"></i>Frozen, no frames <b>' + counts.f + ' s</b></span></div>');
    return out.join("");
  }

  function buildHtml(s, v) {
    var badge = { ok: ["GOOD", "#1a7f37"], warn: ["MARGINAL", "#9a6700"], fail: ["POOR", "#cf222e"] }[v.overall];
    var canvas = (s.gpu.canvases || []).filter(function (c) { return c.webgl && c.w > 100; })[0];
    var rows = [
      ["While rendering — median frame rate", s.active_fps_p50 + " fps"],
      ["While idle — median frame rate", s.idle_fps_p50 + " fps"],
      ["Whole-session median (diluted by idle)", s.fps_p50 + " fps"],
      ["1% low frame rate", s.fps_p05 + " fps"],
      ["Frame time p50 / p95 / p99", s.frame_ms_p50 + " / " + s.frame_ms_p95 + " / " + s.frame_ms_p99 + " ms"],
      ["Worst frame", s.frame_ms_max + " ms"],
      ["Session — foreground / hidden / total", s.foreground_seconds + " / " + s.hidden_seconds +
        " / " + s.seconds + " s"],
      ["Time hung (no frames, main thread blocked)", s.frozen_seconds + " s of " +
        s.foreground_seconds + " s on screen"],
      ["Longest freeze", s.worst_freeze_s + " s"],
      ["Draw calls / frame while rendering", s.active_draws_per_frame.toLocaleString() +
        "  (peak " + s.active_draws_per_frame_max.toLocaleString() + ")"],
      ["Triangles / frame while rendering", (s.active_tris_per_frame / 1e6).toFixed(2) + " M" +
        "  (peak " + (s.active_tris_per_frame_max / 1e6).toFixed(2) + " M)"],
      ["Main thread blocked", s.long_task_supported
        ? s.blocked_pct + "% — " + s.long_tasks + " long tasks, worst " +
          (s.long_task_ms_max / 1000).toFixed(1) + " s" : "not supported"],
      ["JS heap start / peak / end", (s.heap_mb_start || "?") + " / " + Math.round(s.heap_mb_peak || 0) +
        " / " + (s.heap_mb_end || "?") + " MB"],
      ["Texture data uploaded", s.texture_mb_uploaded + " MB"],
      ["GPU", s.gpu.renderer || "unknown"],
      ["CPU threads / device memory", s.hardware_concurrency + " / " + (s.device_memory_gb || "?") + " GB"],
      ["Canvas backing store", canvas ? canvas.w + " x " + canvas.h + " at DPR " + s.device_pixel_ratio : "n/a"],
    ];

    return "<!doctype html><meta charset='utf-8'><title>Real-app performance report</title>" +
      "<style>" +
      "body{margin:0;background:#fff;color:#1f2328;font:15px/1.6 -apple-system,BlinkMacSystemFont," +
      "'Segoe UI',Helvetica,sans-serif}" +
      ".w{max-width:900px;margin:0 auto;padding:40px 24px 80px}" +
      "h1{font-size:25px;margin:0 0 6px}h2{font-size:17px;margin:34px 0 10px;" +
      "border-bottom:1px solid #d1d9e0;padding-bottom:6px}" +
      ".bg{display:inline-block;color:#fff;font-weight:700;padding:6px 15px;border-radius:99px}" +
      ".card{background:#f6f8fa;border:1px solid #d1d9e0;border-radius:8px;padding:16px;margin:14px 0}" +
      ".lg{display:flex;gap:20px;flex-wrap:wrap;font-size:12.5px;color:#59636e;margin-top:10px}" +
      ".lg i{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;" +
      "vertical-align:-1px}.lg b{color:#1f2328}" +
      "table{border-collapse:collapse;width:100%;font-size:14px}" +
      "td,th{text-align:left;padding:8px 11px;border-bottom:1px solid #d1d9e0}" +
      "th{color:#59636e;font-size:11px;letter-spacing:.08em;text-transform:uppercase}" +
      "td:last-child{font-variant-numeric:tabular-nums;white-space:nowrap}" +
      ".f{display:flex;gap:11px;padding:11px 0;border-bottom:1px solid #eaeef2}" +
      ".tag{font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;color:#fff;" +
      "min-width:48px;text-align:center;height:fit-content}" +
      ".math{font-family:ui-monospace,monospace;font-size:14px;line-height:2}" +
      ".math div{display:flex;justify-content:space-between;gap:24px}" +
      ".math .tot{border-top:1px solid #d1d9e0;margin-top:8px;padding-top:8px;color:#cf222e;font-weight:600}" +
      "small{color:#59636e}</style><div class='w'>" +
      "<h1>Real-app performance report</h1>" +
      "<p><small>" + esc(s.url) + "<br>" + new Date().toString() + "<br>" + esc(s.user_agent) + "</small></p>" +
      "<p><span class='bg' style='background:" + badge[1] + "'>" + badge[0] + "</span></p>" +

      "<h2>The session, second by second</h2><div class='card'>" + timeline(s) + "</div>" +

      "<h2>Findings</h2>" + v.findings.map(function (f) {
        var c = { ok: "#1a7f37", warn: "#9a6700", fail: "#cf222e" }[f.level];
        var n = { ok: "PASS", warn: "WARN", fail: "FAIL" }[f.level];
        return "<div class='f'><span class='tag' style='background:" + c + "'>" + n +
          "</span><span><b>" + esc(f.title) + "</b><br><small>" + esc(f.detail) + "</small></span></div>";
      }).join("") +

      (s.active_draws_per_frame ? "<h2>Where the frame budget goes</h2><div class='card math'>" +
        "<div><span>Frame budget at 60 fps</span><span>" + s.frame_budget_ms + " ms</span></div>" +
        "<div><span>Draw calls per frame while rendering</span><span>" +
        s.active_draws_per_frame.toLocaleString() + "</span></div>" +
        "<div><span>Cost per WebGL draw call, CPU side</span><span>~2-5 us</span></div>" +
        "<div class='tot'><span>Submission alone, before any GPU work</span><span>" +
        s.submit_ms_low + " - " + s.submit_ms_high + " ms</span></div></div>" : "") +

      "<h2>Measurements</h2><table><tr><th>Measurement</th><th>Value</th></tr>" +
      rows.map(function (r) { return "<tr><td>" + esc(r[0]) + "</td><td>" + esc(r[1]) + "</td></tr>"; }).join("") +
      "</table>" +
      "<p><small>Captured with lsprobe. Per-frame figures are computed only over seconds in which the " +
      "3D view actually drew; averaging those across idle time is what makes a stuttering application " +
      "look healthy. A gap in the frame timeline counts as a hang only when the tab was on screen and " +
      "a Long Task covers it - a backgrounded tab produces an identical gap and is excluded. " +
      "Raw JSON downloaded alongside this file.</small></p></div>";
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
