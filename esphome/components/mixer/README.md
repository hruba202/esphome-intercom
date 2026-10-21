# ESPHome mixer compatibility adapter

The mixer audio runtime follows upstream, including its per-source playback
counters and the existing task stop/start lifecycle. Remaining differences are
listed in [UPSTREAM.md](UPSTREAM.md). Runtime sources retain GPLv3 and Python
configuration retains MIT licensing; see the included LICENSE.

This adapter keeps the native configuration and speaker interfaces while
using native automation bindings from ESPHome 2026.10.0. It is selected by the
shared playback package for full media profiles.

The upstream [pending restart fix](https://github.com/esphome/esphome/pull/19368)
preserves a new start request while the previous mixer task is shutting down.
The closed PR #21053 contribution queue and forced-output restart are no longer
used. No additional mutex is taken in the output callback.

Normal source pauses are handled inside the mixer without pausing the shared
output. Forced output stop on pause or after the stopping timeout remains the
upstream recovery behavior; it is not presented as an ordinary playback path.

ESP Audio Stack separately rejects new writes while its asynchronous ring reset
is pending. That ownership rule remains in Audio Stack and is not replaced by
another mixer shutdown mechanism. See [Sendspin compatibility](../sendspin_compat/README.md)
for the separate source/dependency adapters.
