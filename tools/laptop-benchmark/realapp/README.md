# Real-app performance tools

Measures **the actual application** — Livspace Parametric / Canvas, Coohom, or
any WebGL web app — running in **your** browser, on **your** laptop, inside
**your** logged-in session.

The synthetic benchmark in the parent directory tells you what the machine can
do. This tells you what the app actually achieves on it. For a "can this laptop
run Parametric?" decision you want both, and this one is the answer.

## Option A — `parametric-sim.html` (no login, no console, no pasting)

**Double-click the file.** It opens in your browser and runs itself.

A standalone stand-in for a browser-based interior configurator — Livspace
Parametric, Coohom and the like. Use it when you cannot instrument the real app:
no login, no DevTools, no IT policy in the way. It sweeps four quality levels,
about 90 seconds in total, and tells you the heaviest design your machine holds
at 60 fps and at 30 fps.

What it reproduces, deliberately:

- A room shell plus many separately-drawn furniture modules — 67 draw calls at
  Low rising to 589 at Ultra. Configurators batch poorly, and that is the point.
- Real tessellated geometry: rounded-edge carcasses and door panels, turned
  handles, subdivided walls and floor. 0.02M triangles per frame at Low rising
  to **1.18M at Ultra**, which is the range a production configurator occupies.
- Textured, specular, normal-perturbed surfaces with three point lights, so
  fragment cost is realistic rather than a flat colour the GPU shrugs off.
- A continuously orbiting camera, as a user inspecting a design.
- An **"add module" edit every 5 seconds**: synchronous geometry generation plus
  an O(n²) constraint relaxation across every module in the room, on the main
  thread. This is what makes a configurator feel like it has hung, and because
  it is O(n²) it gets worse as the design grows — exactly as the real thing does.
- A live BOQ side panel that re-renders on every change.

It reports mean fps, p95/p99 frame time, jank percentage, worst edit stall, and
peak JS heap per level, with a GOOD / MARGINAL / POOR verdict, and offers the
whole thing as a downloadable HTML file.

```
./parametric-sim.html?secs=30      # longer per level, steadier numbers
```

### It is a model, not Parametric

It matches the *resource shape* of a configurator — draw-call-heavy, fragment-
bound, with synchronous main-thread edit spikes. It does not run Livspace's
actual scene, shaders or asset pipeline, so treat it as a capability test for
the machine, not a prediction of exact fps in Parametric. For the real thing,
use Option B.

## Option B — `probe.js` (measures the real app)

### How to run the probe

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

6. It stops on its own and downloads two files: a complete HTML report and the
   raw JSON. `__lsprobe.stop()` ends it early; `__lsprobe.status()` prints a
   live summary without stopping.

The HTML report is self-contained and needs no further analysis. It carries a
second-by-second timeline of the session, the frame-budget arithmetic, the
findings, and the full measurement table.

### Why the report splits idle from rendering

A 3D app only redraws when something changes. Averaging frame rate across the
seconds where it sat still is what makes a stuttering application look healthy
— in one real capture the session median read 57 fps while the median *while
rendering* was 12.8. Every per-frame figure in the report is therefore computed
only over seconds in which the view actually drew, and the report shows both
numbers side by side so the gap is visible rather than hidden.

The timeline also marks **frozen** seconds — gaps where the page produced no
frame at all. These never appear in an average, and they are usually the thing
users are actually complaining about.

### Long captures

Set the duration before pasting the probe:

```js
window.__LSPROBE_SECONDS = 1800     // 30 minutes
```

A long capture answers one thing a short one cannot: **whether the JS heap
plateaus or keeps climbing**. Three minutes shows a number going up and cannot
tell a working set from a leak. The report needs about five minutes of
on-screen time before it will call the trend at all, and it reports growth per
minute of *foreground* time — wall clock would let a spell in the background
flatten a real leak into a gentle slope.

Other things that only a long run surfaces: whether the worst blocking task
recurs or fires once, whether frame pacing degrades as the tab ages, and
whether draw-call counts creep as more of the design is touched.

While it runs it prints a one-line heartbeat to the console every minute, so a
half-hour capture does not look like a hung tab. Switching away from the tab is
fine — hidden time is recorded separately and excluded from every ratio.

Label what you are doing as you go; over half an hour this is what makes the
result readable:

```js
__lsprobe.mark("orbiting")
__lsprobe.mark("adding wardrobe")
__lsprobe.mark("switching to elevation")
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
