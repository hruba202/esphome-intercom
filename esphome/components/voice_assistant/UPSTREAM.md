# ESPHome Voice Assistant fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/voice_assistant`.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/voice_assistant esphome/components/voice_assistant
```

## Local patch

1. `tts_playback_start_timeout`

   Files:
   - `__init__.py`
   - `voice_assistant.h`
   - `voice_assistant.cpp`

   Reason: slow TTS engines can take longer than ESPHome's fixed 2 second
   playback-start timeout. The fork keeps upstream's 2 second default, exposes a
   YAML option, and maintained full voice packages set it to 35 seconds.

   Upstream [PR #18707](https://github.com/esphome/esphome/pull/18707) was merged
   on October 13, 2026. Its option is not in stable 2026.9.1 or beta 2026.10.0b4.
   The remaining completion, cancellation and late-event changes below are
   separate from that merged timeout option; merging it alone does not retire
   the whole adapter.

## Current diff summary

```diff
__init__.py
+ CONF_TTS_PLAYBACK_START_TIMEOUT schema/codegen option

voice_assistant.h
+ setter and tts_playback_start_timeout_ member

voice_assistant.cpp
+ configurable deadline while waiting for playback to start
+ retain upstream use of the configured duration for the shared playback timer
+ preserve completed or aborted playback across late TTS events
+ deliver the streaming-start hook before submitting the media URL
+ acknowledge an interrupted announcement once so Home Assistant can complete its service call
```

After updating ESPHome, re-run the diff above and update this file before
release.

## Alignment checked on 2026-10-18

The response lifecycle is proposed in
[PR #21050](https://github.com/esphome/esphome/pull/21050). The local
`request_stop()` and `on_event()` now follow that candidate, retaining the
string scheduler IDs required by ESPHome 2026.9.1. This includes stopping a
queued announcement while the microphone is stopping, preserving successful
completion when Stop follows playback completion, ignoring cancelled deferred
TTS work, and running the synchronous streaming hook before submitting audio.

The candidate also reports a media player that never starts as
`tts-start-timeout`, aborts that response, and acknowledges failure rather than
success. A fatal pipeline error prevents later queued TTS work from starting.

The startup/stall deadline split is already proposed by another author in
[PR #19408](https://github.com/esphome/esphome/pull/19408). It is outside this
port; the local provider now retains upstream use of the shared configured
timeout. PR #21050 remains draft. Its qualification covers response lifecycle
and hook ordering, not first-PCM detection or cancellation of stalled network
connections in a media source.

Other source differences include older automation/code-generation APIs,
`fill(..., false)`, log-tag syntax and numeric-versus-string scheduler IDs.
They must be compared semantically before removing this compatibility provider.
Opening or updating a PR does not automatically change YAML component sources.
