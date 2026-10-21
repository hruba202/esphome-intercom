# Retiring the compatibility forks

Sources synchronized on 2026-10-19 against ESPHome dev
`2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`, with the maintained build baseline now set to
ESPHome 2026.10.0. This is a local source baseline, not a published release or
physical-device qualification.

The goal is to use native components whenever they cover the behavior the
device needs. Audio Stack, VoIP Stack and Runtime Controller are our components;
this inventory concerns copied upstream components and dependency patches.

## Already removed or no longer selected

- The private SPI and HTTP ring-buffer providers were replaced with native
  ESPHome after [#18699](https://github.com/esphome/esphome/pull/18699) and
  [#18708](https://github.com/esphome/esphome/pull/18708). The obsolete private
  `ring_buffer` provider is also gone.
- The entire local `speaker` provider and its legacy `speaker.media_player`
  implementation have been removed. Fourteen profiles had already stopped
  selecting the identical local speaker base. No maintained YAML used the
  custom `pause_releases_pipeline` option; the last legacy player configuration,
  the Spotpear phone-only ringtone, now also uses `speaker_source` and its
  local-file source. This retirement does not claim that
  the legacy extension was merged upstream. Custom YAMLs must follow the
  [migration instructions](BREAKING_CHANGES.md#development-firmware-retired-speaker-fork).

## Current source alignment

The 2026-10-19 consolidation uses ESPHome dev
`2d0e3faa3d1282d1ec6928d4e76b62db3d37434c` as the common source baseline. A merged PR is not necessarily available
in the stable ESPHome release selected by a user.

The authoritative mapping is [upstream_sources.json](../esphome/upstream_sources.json).
Each copied provider has an UPSTREAM.md identifying functional patches and
stable-core compatibility. The source diff can be regenerated directly:

```bash
python scripts/export_fork_diff.py mipi_dsi --upstream ../esphome --output mipi-dsi.patch
```

Use an ESPHome checkout containing the pinned revision. The command only reads
both trees and writes the requested patch. It does not fetch, commit, push or
publish. Compatibility code for older cores must be separated from an upstream
proposal, rather than submitted indiscriminately.

| Provider | Remaining work |
| --- | --- |
| micro_wake_word | Native affinity where available, legacy task and automation adapters otherwise. Optional upstream internal RAM and buffer-duration settings retain upstream defaults. |
| voice_assistant | Existing draft #21050 plus stable API adapters. No first-PCM or HTTP/TLS experiment. |
| mixer | Runtime aligned with #21053's shared queue; old per-source timeline removed. |
| resampler | #21055 scheduling, older ring-buffer progress and task-creation compatibility. Native microphone resampling is also retained. |
| audio | Current primitives plus host simulator and stable API compatibility. Host support is not a proposed public PR. |
| speaker_source | Pending cancellation, pipeline STOP ownership and worker-exit guard; upstream volume/mute fixes with stable-core silence compatibility. |
| mipi_dsi | Panel settling, transfer ownership and capture access; current upstream drawing/model changes retained. Physical P4 qualification remains required. |
| GMF AI Audio | Runtime source comparison against 255fb531 confirms upstream changes already present; remaining differences are the callback proposed in #73 and ESP-SR 2.6.0 selection. |
| Sendspin compatibility | Adapter reviewed for pinned 0.7.2, 0.9.3 and 0.9.5 sources. This does not migrate firmware to the new protocol. |
| ota | Native update lock and progressive erase; retained watchdog boundaries during final image validation. |
| audio_http_compat | MIME alias pending micro-decoder #27; no task or parallel decoder. |

The removed first-PCM experiments are preserved outside the active source and
test collection. No rejected experimental path is part of the synchronization.

### Keep playback providers together

Audio Stack backend packages explicitly select `resampler` from
`audio_stack_components_source`, alongside `esp_audio_stack`. Merely selecting
the audio backend does not override ESPHome's native resampler automatically.
The matching adapter bounds writes to the input ring's capacity, so an oversized
decoder block waits or makes partial progress instead of repeatedly retrying an
impossible write. The package composition tests guard this provider selection.
Fully native ESPHome profiles continue to use the native resampler.

For firmware qualification, compare the generated component sources with their
intended providers as well as recording repository SHAs. A local fork existing
in the checkout is not proof that a particular board compiled it.

## Preserve the P4 panel fix

The local Waveshare 10.1-inch JD9365 fix waits an additional 120 ms after
`SLEEP_OUT` and 20 ms after `DISPLAY_ON`. The checked stable and dev driver
wait only 10 ms after `SLEEP_OUT`. Init-sequence delay records add to the
driver waits, so compare the final command timeline, not just one constant.
The historical physical symptom was three corrupted color bands.

This should be addressed through the panel's documented initialization timing,
with a narrow upstream change. It must be reviewed separately from concurrent
LVGL/video submission and DMA completion ownership. A replacement must preserve
buffer lifetime until completion, including driver-error paths, without a
second display owner.

Before removing the provider, compile the P4 renderer against the chosen
display/core API and repeat cold initialization, idle presentation, video and
hangup on the physical panel. No new P4 display test was performed for this
inventory; a source diff or successful S3 build does not qualify its screen.

## Order of work

1. Remove unnecessary providers where the maintained configurations already
   use native-equivalent code. Document migration for custom legacy settings.
2. Once a supported release contains the MWW core and component APIs, replace
   that provider and its copied task helper together.
3. Complete the outstanding playback, resampler and GMF changes as focused
   upstream contributions, keeping the existing negative/lifecycle tests.
4. Upstream and qualify the P4 timing and submission requirements separately.
5. Upgrade matched ESPHome/library versions, rerun configuration, native,
   firmware and relevant physical checks, then delete superseded local code
   and external-component entries in the same change.

Moving copied code into a differently named adapter is not retirement. Keep
one active implementation for each operation throughout the transition.

## ESPHome 2026.10 baseline

The maintained minimum is 2026.10.0. Old/new API selectors for ring-buffer
handles, speaker mute/silence and worker owner selection are removed. Mixer and
playlist actions use native `register_apply_action`; HTTP uses its native IDF
owner. The existing core-affinity creation adapter remains because the released
2026.10 `StaticTask::create` still has no core parameter. This is one concrete
adapter for the supported core, not a second task or compatibility matrix.
Sendspin's 0.7 source matcher is retired. Functional STOP, DSP and P4 panel
changes are evaluated independently of this minimum-version change.
