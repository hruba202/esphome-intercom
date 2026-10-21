# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/mixer`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

The audio runtime follows upstream: per-source atomic playback counters and the
existing mixer task lifecycle. The shared PlaybackQueue, callback mutex,
metadata backpressure and forced-output restart from the closed PR #21053 have
been removed. No alternative accounting engine has been added.

The prior tests demonstrated arbitrary intermittent-contribution and forced
output-stop cases with host adapters. They did not demonstrate those cases
through the normal upstream Sendspin/Speaker Source producers. Those results
are not a reason to retain the heavier algorithm for all mixer writes.

Automation bindings now use the official ESPHome 2026.10 register_apply_action
implementation. The mixer runtime uses upstream counters and lifecycle. Its
only remaining C++ delta adapts fill() to the newer coordinated audio provider;
there is no contribution metadata queue or callback mutex. PR #21053 was closed
and is not part of this baseline.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
