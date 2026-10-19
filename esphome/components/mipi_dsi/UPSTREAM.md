# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/mipi_dsi`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

Upstream panel definitions, flash-resident initialization tables, pixel-format
handling, clipping, rotation and dirty-region updates are retained.

The remaining functional adaptations are the qualified settling delays,
serialized submit/completion ownership, failure handling before waiting for a
DMA callback, and framebuffer access used only by the diagnostic capture tool.
The old periodic flush counters have been removed.

The retained driver waits are 120 ms after SLEEP_OUT and 20 ms after DISPLAY_ON.
The generated sequence adds its own waits; for the maintained JD9365 profile
these make 130 ms and 30 ms respectively. This preserves the previous panel
behavior; it is not a newly qualified universal timing policy for every panel.

One bulk-drawing implementation serves the old public signature and the current
Display compatibility bridge. Stable code generation uses a PROGMEM array when
shared_progmem_array is unavailable. The clipping predicate is the same as
upstream, inlined for older cores without is_point_clipped().

Compilation and host transfer tests do not qualify physical colors, orientation,
LVGL/video concurrency or cold initialization. Repeat those on the P4 before a
firmware promotion; no device was flashed during this consolidation.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
