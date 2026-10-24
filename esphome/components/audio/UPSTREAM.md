# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`,
component `esphome/components/audio`. Checked on 2026-10-19.
The machine-readable reference is `esphome/upstream_sources.json`.

## Remaining adaptations

The source follows upstream audio conversions, decoder changes and readable-buffer APIs.
Local differences support the host simulator and tests and stable code-generation/HTTP APIs.
The host adapters are project test infrastructure, not an upstream PR proposal.

The local `AudioResampler` also accepts an internal hint for an explicitly mono
Mixer renderer that discards the right channel. During sample-rate or bit-depth
conversion, only the left channel passes through ART and quantization. Its result
is duplicated into the existing stereo layout before the renderer, preserving
frame credits, partial-byte writes and interleaved gain-ramp progression. This
is channel selection, not averaging. Equal-format playback keeps its existing
task-free bypass, and stereo renderers retain both channels.

The coordinated resampler facade enables the hint only after native final
validation resolves the actual mono Mixer. The owned header advertises this
extension with `ESPHOME_AUDIO_HAS_MONO_CHANNEL_PRUNING`; the facade uses the
ordinary four-argument call with the official ESPHome header. No audio-library
fork or additional user option is introduced. This adaptation is not part of
speaker task-affinity PR #21055.

`tests/test_resampler_channel_pruning_runtime.py` compiles the production wrapper
with pinned esp-audio-libs 4.1.0 ART, PCM conversion, mixing and gain ramps. It
checks identical rendered left PCM and byte credits, half the FIR work for the
eligible stereo conversion, reused ring exposures, partial writes and restart.
Those host checks do not establish ESP scheduling or acoustic performance.

## Export the source difference

Use `scripts/export_fork_diff.py` from the project root with an ESPHome checkout
containing the recorded commit. The export includes functional changes and
stable-core compatibility adapters; separate the latter before an upstream PR.
Existing licenses and upstream authorship remain applicable.
