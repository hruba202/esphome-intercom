# Retiring the compatibility forks

Status checked on October 17, 2026 against ESPHome 2026.9.1 and upstream dev
`1a28a9220ba1f77f8d9576562adb0baacba3ed0c`. The latest published beta checked was
2026.10.0b4. A merged PR is not necessarily present in either release.

PR links below were refreshed on October 18; the version/source comparison
above remains the stated October 17 snapshot.

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

## What still blocks removal

| Provider | Current difference | Upstream status and removal condition |
| --- | --- | --- |
| `micro_wake_word` | Inference task affinity and a temporary local StaticTask helper | [#20126](https://github.com/esphome/esphome/pull/20126) merged October 14. Neither 2026.9.1 nor 2026.10.0b4 includes it. Use a release containing both the shared core API and the component change, then remove the local helper and provider together. |
| `voice_assistant` | Configurable TTS startup deadline, completion/abort tracking, late-event handling and interruption acknowledgement | [#18707](https://github.com/esphome/esphome/pull/18707) merged October 13, but its option is absent from 2026.9.1 and 2026.10.0b4. The response-lifecycle and preparation-hook changes are proposed in draft [#21050](https://github.com/esphome/esphome/pull/21050); the timeout PR alone is insufficient. |
| `speaker_source` | Playback accounting, pending-source cancellation, stop dispatch and worker-exit verification | [#19499](https://github.com/esphome/esphome/pull/19499) is merged and included in 2026.10.0b4. [#19501](https://github.com/esphome/esphome/pull/19501) remains open. The worker-exit issue also has [ESP-IDF #19123](https://github.com/espressif/esp-idf/pull/19123), still open. Compare the complete local stop path after those land. |
| `mixer` | Per-source playback credit and coordinated stop/restart of a shared output | The [restart fix #19368](https://github.com/esphome/esphome/pull/19368) is merged. The remaining accounting and shared-output changes are proposed in [#21053](https://github.com/esphome/esphome/pull/21053). Keep the provider until the selected release covers the complete behavior. |
| Audio Stack's `resampler` provider | Explicit core/priority, bounded writes and backpressure during stop | Scheduling is proposed in [#21055](https://github.com/esphome/esphome/pull/21055), and shared ring-buffer write progress in [#21058](https://github.com/esphome/esphome/pull/21058). Compare both released changes before removing the local compatibility code. |
| `audio` | Host simulation support and newer dependency selection | Local development selects esp-audio-libs 4.1.0 and micro-flac 0.3.0; 2026.9.1 selects 3.2.1 and 0.2.0. Dev contains the newer dependencies but still restricts the component to ESP32. Treat host support separately from the firmware dependency update. |
| `mipi_dsi` | Panel settling delay, serialized display submissions, completion ownership and diagnostic framebuffer access | The three-band initialization correction remains absent from stable and dev. New upstream drawing APIs do not establish equivalent transfer ownership. See the panel-specific section below. |
| GMF fork | Immediate processed PCM output and ESP-SR 2.6.0 selection | Stack placement is already upstream in [dabc2873](https://github.com/espressif/esp-gmf/commit/dabc2873b0987206080e538784e8afccb313e0f4), included in release v1.1, despite [PR #71](https://github.com/espressif/esp-gmf/pull/71) still appearing open. The output-callback API is proposed in [#73](https://github.com/espressif/esp-gmf/pull/73). The remaining version constraint is separate: upstream GMF AI Audio 1.1.1 still constrains ESP-SR to `2.5.*`, while the validated local build selects 2.6.0. |
| `audio_http_compat` | `audio/vnd.wave` MIME alias | [micro-decoder #27](https://github.com/esphome-libs/micro-decoder/pull/27) remains open. Remove the adapter after the selected decoder release recognizes the MIME alias. |
| `sendspin_compat` | Worker-memory flags and partially late chunk handling | Stable ESPHome selects sendspin-cpp 0.7.2. Dev selects 0.9.3 and the library's latest release is 0.9.5. Upgrade the ESPHome adapter and library as a matched pair; compare both patches instead of applying the old replacement units to a new API. |

The status above is a source/release comparison, not a promise that every
upstream beta is compatible with the current device packages. In particular,
current dev changes the display bulk-draw signature. Swapping only one display
file into the older core is not an equivalent upgrade.

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
