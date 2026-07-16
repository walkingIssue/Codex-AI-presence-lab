# Third-party notices

This local-only avatar bundle vendors `pixi.js` 6.5.10, `@pixi/unsafe-eval` 6.5.10, and `pixi-live2d-display` 0.4.0 (MIT license texts in `vendor/`).

The bundled Live2D Cubism Core is the official Web distribution from `cubism.live2d.com` and remains subject to the Cubism SDK terms. `vendor/cubism4-local.js` is a local performance fork of the `pixi-live2d-display` Cubism 4 bundle. It expands the opt-in clipping grid from 4x4 to 5x5 cells for high-mask-count models; caches Cubism ID lookups, parameter snapshots, draw colors, and parsed motion segments; replaces mask-matrix allocation with in-place math; removes transient arrays, typed-array tail copies, and vector allocations from per-frame motion and physics paths; and interpolates a capped 30 Hz physics simulation into the renderer frame loop without changing the model or its source assets.

This bundle intentionally excludes VTube Studio configuration, plug-ins, scene items, pinned-item state, expressions, and motions. It makes no network requests at runtime.
