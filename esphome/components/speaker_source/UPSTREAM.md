# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/speaker_source`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

Current upstream frame reservation and volume/mute behavior are retained.
Pending-source cancellation, immediate pipeline-scoped STOP, worker-exit
verification and explicit queue diagnostics remain local adaptations.

Pending-source cancellation is the correctness fix proposed in #19501.
Immediate STOP is a stronger local maintenance policy: the native OTA data
handler occupies the main loop, so queued commands cannot run before hardware
shutdown in its start callback. It is not classified as another demonstrated
upstream correctness defect. Source completion retains upstream `finish()`;
the maintenance composition stops its input buffers before shared hardware.

ESPHome 2026.10 owns the speaker silence threshold and mute behavior directly.
The legacy speaker_compat.h detector is removed. Playlist actions use native
register_apply_action bindings.
The abandoned first-PCM and HTTP/TLS experiments are not included.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
