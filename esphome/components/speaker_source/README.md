# ESPHome playback adapter

Synchronized with the upstream revision and remaining adaptations recorded in
[UPSTREAM.md](UPSTREAM.md). Runtime sources retain GPLv3 and Python configuration
retains MIT licensing; see the included LICENSE.

This adapter keeps the native configuration and speaker interfaces. It is
selected by the shared playback compatibility package for full media profiles.
The playback accounting changes and dependency compatibility are described in
[Sendspin compatibility](../sendspin_compat/README.md).

Stop commands also target a source that is still opening its HTTP stream.
An explicit stop cancels that pending request and does not advance the playlist.
STOP is dispatched immediately on the same main-loop owner, even if a queued
play request cannot progress. Superseded commands and URI allocations are removed
only for that pipeline; other pipeline commands retain their order. This also
releases the existing HTTP decoder workers before a blocking OTA transfer begins.
Source completion uses upstream `finish()` semantics. Firmware maintenance
cancels its resampler and mixer inputs through ordinary `speaker.stop` actions
before closing shared hardware, so a later decoder completion cannot strand
audio in an input whose output has disappeared. Ordinary playback can drain
normally; maintenance owns the decision to discard its buffered audio.

Worker shutdown also guards ESP-IDF's `pthread_join` against unrelated task
notifications. ESPHome uses task notifications to wake its main loop, including
while an HTTP reader is stopping. The adapter checks the worker's actual exit
state before releasing its resources. It adds no task, media buffer or polling
loop and preserves the upstream source outside that completion check.

Playback accounting is included in ESPHome 2026.10 through
[#19499](https://github.com/esphome/esphome/pull/19499). It is no longer a local
compatibility difference. The remaining proposals are:

- [Pending-source cancellation](https://github.com/esphome/esphome/pull/19501).
- [ESP-IDF join completion](https://github.com/espressif/esp-idf/pull/19123),
  included in upstream master as
  [602fdf5e](https://github.com/espressif/esp-idf/commit/602fdf5e5a66ada1fd50d7bf0f9975f74ca65713).
  The checked 5.5.5 and 5.5.6 releases still need the compatibility guard.

Recheck these differences against the pinned upstream baseline. An open
proposal is not a replacement for a released upstream fix.

## Diagnose a stalled control queue

A full control queue now logs its first waiting command and the state of both
playback pipelines. Keep the complete block: queue depth, active/target/pending/
stopping source states, speaker state and pending frame count. The head command
codes are 0 (replace URI), 1 (enqueue URI), 2 (advance playlist), 3 (play current)
and 4 (player command).

During an investigation, the same snapshot can be requested from a main-loop
lambda with `id(speaker_media_player).dump_diagnostics()` (use your player ID).
For example, an optional `interval` automation can request it every 60 seconds.
Use logger level INFO or more verbose. The snapshot does not clear the queue,
stop playback, or log media URLs. It adds no periodic work unless you configure it.

A source's pending frame count alone is not proof of a blocked output: after its
IDLE notification, final output callbacks may no longer be attributed to that
source. Compare it with speaker state and the mixer/output buffers.
