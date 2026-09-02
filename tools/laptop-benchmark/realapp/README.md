# Real-app performance probe

Measures **the actual application** — Livspace Parametric / Canvas, Coohom, or
any WebGL web app — running in **your** browser, on **your** laptop, inside
**your** logged-in session.

The synthetic benchmark in the parent directory tells you what the machine can
do. This tells you what the app actually achieves on it. For a "can this laptop
run Parametric?" decision you want both, and this one is the answer.

## How to run it

1. Open the app and log in. Get to the **3D view** with a real design loaded —
   an empty project measures nothing.

2. Open DevTools: **⌥ ⌘ I**, then the **Console** tab.

3. Chrome blocks pasting into the console the first time. If it warns you,
   type `allow pasting`, press Enter, then continue.

4. Paste the whole of [`probe.js`](probe.js) and press Enter. You will see:

   ```
   [lsprobe] recording for 180s - USE THE APP NOW
   ```

5. **Use the app normally for three minutes.** Orbit, pan, zoom, add a module,
   switch floor to elevation, open the BOQ. Exercise the things that feel slow.
   Optionally label what you are doing as you go:

   ```js
   __lsprobe.mark("orbiting")
   __lsprobe.mark("adding wardrobe")
   __lsprobe.mark("switching to elevation")
   ```

6. It stops on its own and downloads two files: an HTML report and the raw
   JSON. `__lsprobe.stop()` ends it early; `__lsprobe.status()` prints a live
   summary without stopping.

To record for longer, set the duration before pasting:

```js
window.__LSPROBE_SECONDS = 600
```

## What it measures

| | Why it matters |
| --- | --- |
| Frame rate — median and 1% low | The 1% low is what you feel. A 60 fps median with a 12 fps 1% low feels broken. |
| Frame time p50 / p95 / p99, worst frame | Pacing. Stutter shows up here even when the average looks healthy. |
| Freezes over 250 ms | The "it hung" moments. |
| **Main-thread long tasks** | Usually the real culprit. If JavaScript blocks the main thread, input stops responding and a faster GPU cannot help. |
| Draw calls per frame | High counts are an app batching problem, CPU-side, not a GPU limit. |
| Triangles per frame | The actual geometric load of your design. |
| Texture and buffer bytes uploaded | Asset weight pushed to the GPU after the probe starts. |
| Peak JS heap | Whether a bigger design will crash the tab. |
| GPU renderer string | Catches Chrome silently falling back to software rendering. |
| Canvas backing resolution vs DPR | Retina rendering is often the cheapest large win available. |

## Reading the result

The verdict is **PASS / MARGINAL / FAIL**, but the finding that matters most is
*which* limit you hit — the fix is completely different for each:

- **"Not using the GPU"** — nothing else in the report means anything. Open
  `chrome://gpu` and fix hardware acceleration first, then re-run.
- **"Main thread is saturated"** — an application problem. Long tasks are
  Livspace's JavaScript, not your hardware. A faster laptop helps far less than
  you would expect; this one goes to the Canvas team.
- **"Frame rate is poor"** with a healthy main thread — a genuine GPU limit.
  This is the case where a better machine actually helps.
- **"Rendering at full Retina resolution"** — halving the render scale usually
  costs very little visually and often doubles the frame rate.

## Honesty about what it can and cannot see

- Texture and geometry counters only capture uploads **after** the probe
  starts. Assets loaded during initial scene load are already on the GPU and
  are not counted. To capture them, paste the probe before opening the design.
- Frame rate is sampled from `requestAnimationFrame`, so it reflects what the
  compositor delivers — which is what you perceive. It is capped by your
  display's refresh rate, so 60 (or 120) is the ceiling, not a failure.
- `performance.memory` is Chrome-only. In Safari or Firefox the heap figures
  are omitted rather than guessed.
- The probe only reads timing counters. It sends nothing anywhere, writes no
  storage, and does not touch app state or credentials. Everything stays in
  your browser; the report downloads to your machine.

## Verification

The probe was validated against a WebGL scene with a known, fixed workload —
10 objects at 8,192 triangles each. It reported 10.1 draw calls per frame and
82,632 triangles per frame against a true 81,920. The instrumentation is
accurate.
