# Cubism renderer performance

The renderer uses one Pixi application ticker per avatar, drives Cubism updates manually, caches parameter indexes and compiled expression plans, and keeps the Cubism physics, motion, masking, and draw hot paths allocation-free. The current quality baseline enables antialiasing, renders at native device density capped at `2x`, and caps model-local physics at 30 Hz while interpolating its outputs into the render loop. These choices apply to every Cubism model materialized by this runtime; no model ID or Higan-specific parameter appears in the optimization path. The renderer deliberately retains WebGL buffer orphaning: a per-drawable `bufferSubData` experiment introduced GPU synchronization stalls and measured worse on the live multi-avatar stack.

## Measurement baseline

The pre-optimization live stack was sampled for eight seconds with two Higan avatars and one built-in avatar. CPU percentages below use the process sampling convention where `100%` is approximately one logical core.

| Scenario | Aggregate process CPU |
| --- | ---: |
| Three idle windows at 20 FPS | 86.09% |
| Two Higan windows active at 60 FPS | 231.77% |

Live validation of the efficiency-first snapshot (`0.75x` density with antialiasing disabled):

| Scenario | Aggregate process CPU | Change from matching baseline |
| --- | ---: | ---: |
| Three idle windows at 20 FPS, before switching the default | 65.04% | -24.5% |
| Two Higan windows active at 60 FPS, final build | 149.81% | -35.4% |
| Three idle windows at the new 60 FPS default, three-sample median | 158.34% | Not comparable to the old 20 FPS idle sample |

The final idle number is the median of three consecutive six-second samples. The active comparison uses the same two bound Higan sessions and aggregate Electron process accounting as the original eight-second stress sample. Those figures preserve the maximum-efficiency checkpoint and do not claim to measure the later native-density, antialiased quality baseline.

Live validation of the native-density, antialiased quality baseline:

| Scenario | Aggregate process CPU | Change from efficiency-first snapshot |
| --- | ---: | ---: |
| Three idle windows at 60 FPS, three-sample median | 159.11% | +0.5% |

This quality sample used the same six-process Electron tree and three consecutive six-second intervals. The near-flat CPU result is consistent with the density and antialiasing cost moving primarily to GPU work; it is not a GPU utilization measurement.

These numbers are a local regression baseline, not a portable capacity rating. Hardware, model complexity, clipping masks, physics rig size, texture resolution, and window count all affect the result.

## Deferred configuration surface

**Setting matrix needs to be reviewed and implemented for future optimization.** This change intentionally does not add a settings UI or configuration contract.

The future review should cover:

- maximum foreground and background FPS;
- idle behavior: continuous, reduced-rate, frozen, or do not render;
- render only the active model or latest speaking model;
- resolution, antialiasing, mask-buffer, and physics-quality tiers;
- plugged-in, battery, integrated-GPU, and discrete-GPU presets;
- global, project, profile, and session-level override precedence;
- visibility, occlusion, minimized-window, and focus-aware scheduling;
- safe automatic degradation and recovery based on measured frame time.

Any future matrix must preserve profile/session isolation and keep model-local renderer controls out of the Voice transport contract.

## Concurrency audit

Electron already creates a separate renderer process for each avatar `BrowserWindow`, and Chromium owns a separate GPU process. That gives multiple avatars process-level CPU parallelism without sharing mutable Cubism state. Inside one avatar, Cubism Core update, physics, and WebGL command submission remain one ordered renderer-thread pipeline.

The Voice host no longer uses `spawnSync` for record start, finish, or cancel. Commands launch asynchronously, retain a five-second kill deadline, and pass through a promise queue so the input state machine stays ordered without blocking Electron's main event loop. Recording writes are asynchronous. The preload also receives its immutable frame policy through `BrowserWindow` arguments instead of synchronous renderer-to-main IPC.

A full Web Worker/`OffscreenCanvas` port is intentionally deferred. It would need to move Cubism Core, physics, resource loading, and the WebGL context together; splitting only physics would add per-frame parameter transfer and synchronization to a pipeline that is already isolated per avatar process. This should be evaluated as a dedicated renderer backend rather than mixed into the settings matrix.

## Research basis

- [PixiJS performance tips](https://pixijs.com/8.x/guides/concepts/performance-tips) recommend disabling antialiasing and reducing renderer resolution when quality permits.
- [Live2D mask preprocessing guidance](https://docs.live2d.com/en/cubism-sdk-manual/ow-sdk-mask-premake-web/) notes the cost of render-target switching in the Web mask path.
- [Live2D physics compatibility guidance](https://docs.live2d.com/4.2/en/cubism-sdk-manual/compatibility-with-cubism-4-2/) documents physics FPS and interpolation as supported model-runtime concepts.
- [Electron's process model](https://www.electronjs.org/docs/latest/tutorial/process-model) confirms one renderer process per `BrowserWindow` and identifies utility processes for genuinely CPU-intensive main-process work.
- [Node's child-process documentation](https://nodejs.org/api/child_process.html) documents that `spawnSync` blocks the event loop until the child exits or is terminated.
