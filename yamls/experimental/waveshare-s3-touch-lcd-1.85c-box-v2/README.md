# Waveshare 1.85C-BOX V2 FULL AEC candidate

This candidate investigates playback wake-word responsiveness on the Waveshare
ESP32-S3-Touch-LCD-1.85C-BOX V2 (SKU 30684). It does not replace the maintained
FULL AFE profile and is not ready for deployment.

## Processing changes

Standalone `esp_aec` replaces `esp_afe`, using `fd_low_cost` by default.
The processed input is the primary microphone plus the existing hardware
playback reference. The board still has two microphones, but this profile
does not provide the dual-microphone processing, noise suppression or VAD
provided by AFE. Assist, MWW, VoIP, Sendspin, HTTP playback and the display
remain configured.

Audio Stack stays on core 1; MWW moves to core 0. Removing the AFE feed/fetch
pipeline changes the competing workload, but neither CPU headroom nor improved
barge-in has been measured on hardware. The AFE-specific `audio_dsp_iram`
linker helper is omitted.

The original AFE YAML assigns MWW, Audio Stack, AFE fetch and Wi-Fi to core 1,
with AFE feed on core 0. This is a reason to investigate scheduling, not proof
that scheduling alone causes missed wake words. Echo cancellation can also
affect the near-end voice during simultaneous playback.

## Animation and flash budget

The animation uses the same eight original poses as the maintained AFE profile:
00, 03, 05, 08, 11, 14, 16 and 19. Resolution remains native 360x360 RGB565.
Source PNGs are unchanged and are not deleted. Forward and reverse animation
durations remain 2300 ms each, with the existing reverse delay and loop timing.

The eight idle images and ten other images require 4,665,600 bytes of raw RGB565
data. ESPHome 2026.9.1's default 16 MB partition layout provides an 8,126,464-byte
application slot, leaving 3,460,864 bytes for code, models, fonts and other data.
This is an asset-budget calculation, not a measured firmware binary size.
The local ESPHome 2026.9.1 / ESP-IDF 5.5.5 build passed with an OTA binary
of 7,527,328 bytes and 599,136 bytes of application-slot headroom.
Static DIRAM usage is 170,383 of 341,760 bytes (49.85%). This does not
measure free heap after startup or peak runtime memory use.

## Validation still required

ESPHome 2026.9.1 configuration validation, C++ generation and full compilation
passed using local component checkouts and placeholder Wi-Fi credentials.
No hardware test or upload has been performed. Before promotion, measure:

- Wake-word recognition during repeatable playback, with and without speech.
- Near-end speech before and after AEC, reference alignment and gain.
- MWW overruns, audio continuity, task scheduling and internal-memory headroom.
- Bidirectional calls, playback interruption, Assist transitions and teardown.
- Display responsiveness with the full animation and concurrent audio.

Use the same stimulus and firmware identity for comparisons. Do not infer
working barge-in from a successful build or from RTP packet counts.

## References

- [Espressif AFE resource benchmarks](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/benchmark/README.html)
- [Espressif acoustic echo cancellation](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/acoustic_echo_cancellation/README.html)

Espressif benchmarks describe their tested processing chains. They are not
measurements of this firmware's combined AEC, MWW, display and VoIP workload.
