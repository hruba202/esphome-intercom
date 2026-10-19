# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/voice_assistant`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

The response lifecycle and intent-hook changes are aligned with draft PR #21050,
patch revision 0bfd7b5146fd86e1510fb988a1326ee29491fe68. No new behavior is added
by this synchronization. Local differences keep stable automation bindings,
string scheduler identifiers and log declarations.

tts_playback_start_timeout keeps the upstream common playback timeout contract;
it does not claim first-PCM detection. The native-player hardware evidence for
the PR is distinct from builds of this consolidated component set.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
