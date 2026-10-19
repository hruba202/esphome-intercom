# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/mixer`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

The runtime uses the shared PlaybackQueue and output-stop ordering proposed in
PR #21053, patch revision b7ffb962548887d5a3652d287a96e2c69ca5b3de.
The superseded per-source PlaybackTimeline implementation has been removed.
Stable automation and log/status bindings remain local compatibility code.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
