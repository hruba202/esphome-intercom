# ESP audit follow-up, 2026-10-21 to 2026-10-22

This records local implementation and validation completed before the user
authorized publication on 2026-10-22. The validated changes are now being
checkpointed on dev and included in the development prereleases. No Home
Assistant deployment or upstream PR belongs to this candidate. Pre-existing
untracked core files were preserved and excluded from publication.

## Source identity and scope

| Repository | Starting dev HEAD |
| --- | --- |
| Intercom | `d36661bfd06666a195faa707549f1a324935111b` |
| Audio Stack | `2d01914e4ebf1cf8583d231f36cd7d2322801bb0` |
| ESP VoIP Stack | `4073cc93ad6ea0aa9db572ec9d44d2269732a380` |
| Runtime Controller | `ce55849e0b2237d6d92877dc6da25e51c5b8b0fd` |

The validation used an uncommitted delta over these starting heads, so the
starting HEAD alone does not identify the tested sources. The local evidence manifest records component-file
SHA-256 values, dirty status, image hashes, configuration hashes and build times.
The builder is official ESPHome 2026.10.0 with ESP-IDF 5.5.6. Generated S3 and P4
core sources were compared with the installed ESPHome package: 58 static core
files per target matched. ESPHome's generated `defines.h`, `version.h` and
`build_info_data.h` are excluded from that static-source comparison.

The application source, native DSP dependency and camera retain their recorded
revisions. Native VoIP DSP tests use esp-audio-libs
`e06373fa41529064cb852e2312a32a8148ddaaac` (4.1.0). The camera pin remains
`0814536988d946e0e697cccef5063b208f6df901`. GMF remains pinned to
`b1e077aa5fd34e937752712f94b1a515d459b116`; it was not patched here.

## Findings corrected

| Finding | Verified cause and correction | Before/after witness |
| --- | --- | --- |
| F1, microphone-only compile | The common connected-identity handler called a speaker-only member. Guard only RX reset with the speaker macro, retaining watchdog reset. | The real signal handler failed to compile without speaker and passed with speaker. Both now pass, and full microphone-only/speaker-only builds link. |
| F2, REFER ringing cancelled by RTP watchdog | The original INVITE can end while the replacement still rings. SIP owns a small atomic projection of whether remote audio is expected; the app watchdog refreshes its baseline only during that wait. SIP deadlines remain active. | The old code triggered termination during the valid wait. The new wire test combines production parser, dispatcher, REFER pump, transaction timers and app watchdog over all four UDP/TCP pairs, with success, rejection and SIP timeout. The receive watchdog becomes effective again after connection. |
| F3, incoming media preparation failure | An incoming transaction was owned while the public FSM was still IDLE, so the old idle shortcut discarded termination. Track that setup ownership and use common termination to send/cache 500, stop media and clear identity. | The old handler sent no final error. The new composed test injects converter failure and a genuinely occupied UDP port into production preparation, checks cleanup and a second call. Socket tests verify cached final-response replay and ACK retirement over UDP/TCP. |
| F4, children RUNNING after I2S failure | The audio owner reached FAILED without reconciling already-running adapters or discarding their PCM. The quiescent audio owner now resets stale PCM; main reconciles children and processor while preserving consumer registrations. | Old code left speaker/microphone RUNNING. TX/RX failure tests now stop both, preserve consumer intent, reject writes during the completion window and permit explicit recovery. One implicit speaker recovery is bounded; repeated failures do not create a restart storm. |

F1-F3 changes belong to ESP VoIP Stack, principally `voip_fsm.cpp/.h`,
`sip_refer.cpp`, `sip_transport.cpp/.h`, `transport.h` and the watchdog in
`voip_stack.cpp`. F4 belongs to Audio Stack's existing lifecycle and
microphone/speaker adapters. No second SIP task, reader, dialog registry or media
engine was introduced. The earlier REFER challenge and retired-transaction
replay fixes were preserved.

The REFER action lifetime was checked against [RFC 3515 section 2.4.4](https://www.rfc-editor.org/rfc/rfc3515.html#section-2.4.4).
The local setup error uses the existing 500 response path, consistent with
[RFC 3261 section 21.5.1](https://www.rfc-editor.org/rfc/rfc3261.html#section-21.5.1).
The diagnostic queue uses the documented [ESP-IDF No-Split acquire/complete and return-item contract](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32/api-reference/system/freertos_additions.html).

The permanent tests compile methods from current production sources. Scheduler,
initial call state and hardware/vendor failure adapters are explicitly fixtures.
They do not establish multicore timing or full DSP quality on an ESP. The F3 TCP
replay test uses the production INVITE validation/replay prefix and fails if it
unexpectedly enters new INVITE negotiation; the negotiation itself is outside
that focused fixture. A proper non-2xx ACK retires the transaction before reuse.

## Upstream findings deliberately excluded

The user's scope excludes F5-F7 where the affected logic belongs to upstream.

- F5: partial GMF construction/destruction is vendor code, identical in the
  compared fork/upstream methods. No additional GMF patch or PR was made.
- F6: the saturated ENQUEUE path matches upstream Speaker Source apart from
  local diagnostics. Its playlist queue was not rewritten.
- F7: the Micro Wake Word pending-start stop path matches upstream. It was
  not modified or worked around here.

These exclusions are not claims that those findings are fixed.

## Optional Audio Stack additions

See [Audio Stack's tuning guide](https://github.com/n-IA-hane/esphome-audio-stack/blob/dev/docs/AUDIO_TUNING.md)
and its `04-dual-mic-downmix.yaml` example.

- `mic_mix: average` combines two physical inputs during existing conversion
  and delivers mono to AEC, mono AFE or no processor. Passthrough remains the
  default. AEC/AFE sources were not modified. Invalid dual-AFE/downmix layouts
  fail validation.
- Same-rate conversion no longer allocates unnecessary channel scratch.
  Rate-changing 32-bit paths extract only selected channels while retaining
  Espressif bit/rate converters. Digital gain and filter order are preserved.
- Optional `input_pga` and `reference_pga` numbers request analog changes on
  the existing codec owner. Saved values take precedence over defaults;
  microphone/reference channels are isolated. An unselected reference channel
  is rejected in configuration. Driver failure restores the last accepted
  setting without overwriting a newer request.
- Optional `esp_audio_capture` exports raw physical microphones, effective
  reference, processor input/output and final microphone output. Formats and
  timelines remain per stream. A bounded PSRAM-preferred queue and temporary
  low-priority sender exist only during capture. Queue pressure produces
  diagnostic gaps or bounded termination, without waiting in the audio task.
- Capture owns an explicit token in Audio Stack's existing consumer registry.
  It does not infer ownership from a void microphone start operation. Tests
  cover a saturated listener count, rejected admission, a producer paused
  during stop, disconnect, mute, format/generation changes and repeated use.
  Sequentially consistent admission/counter operations protect queue retirement
  on weakly ordered targets. Main deletes only a retired worker with no further
  application accesses. No extra I2S reader is created.

All three optional features are absent from compilation when not configured.
The maintained board profiles retain their existing delivery, gain and DSP
choices. The receiver saves separate WAVs plus metadata and fills known missing
sample positions with silence; it does not invent an AFE-internal tap or infer
lost samples from arrival order alone.

## Executed software gates

These are local results, not new GitHub Actions results.

| Gate | Result |
| --- | --- |
| Audio Stack full native/config suite | 202 passed, 0 skipped |
| ESP VoIP full native suite with DSP 4.1.0 | 234 passed, 0 skipped |
| Runtime Controller | 84 passed, 0 skipped |
| Intercom selected firmware runtime tests | 20 passed, 0 skipped |
| Intercom documentation, feature validation and upstream alignment | 21 passed, 0 skipped |

Focused rerun commands identify the individual contracts:

```sh
# ESP VoIP Stack
python -m pytest tests/test_incoming_setup_runtime.py -k optional_speaker
python -m pytest tests/test_media_timeout_behavior.py tests/test_refer_wire_runtime.py tests/test_refer_dialog_runtime.py
python -m pytest tests/test_incoming_setup_runtime.py tests/test_esp_termination_behavior.py
python -m pytest tests/test_incoming_media_failure_runtime.py tests/test_incoming_response_replay_runtime.py

# Audio Stack
python -m pytest tests/test_audio_failure_runtime.py
python -m pytest tests/test_capture_runtime.py tests/test_optional_audio_config.py
```

The final ACK extension also passed its two focused UDP/TCP cases. Lint and
`git diff --check` passed. Counts are kept separate; native cases, parametrized
scenarios and physical cycles are not added into a single qualification number.

Commands were run with the repository Python environment (HA lab interpreter
only for the existing real SIP bench):

```sh
# In Audio Stack
python -m ruff check esphome tests tools
python -m pytest -q --junitxml=audio-native.xml

# In ESP VoIP Stack, with the recorded official DSP checkout
ESP_AUDIO_LIBS_SOURCE=.test-deps/esp-audio-libs python -m pytest -q --junitxml=voip-native.xml

# Full builds, not --only-generate
python -m esphome compile tests/minimal_no_text.yaml
python -m esphome compile tests/minimal_speaker_only.yaml
python -m esphome compile examples/04-dual-mic-downmix.yaml
```

Local build overlays select the actual sibling checkouts and test credentials
or the already-authorized private board configuration. Their hashes and exact
commands are retained in local evidence. No patched ESPHome core or compiler
wrapper was used.

## Complete firmware builds

| Configuration | Static RAM bytes | Linked flash bytes |
| --- | ---: | ---: |
| S3 FULL AFE/Sendspin, original source baseline | 158571 | 2977615 |
| S3 FULL AFE/Sendspin, maintained defaults with fixes | 158571 | 2978327 |
| P4 full landscape JPEG, pre-v3 path | 167052 | 13369468 |
| VoIP microphone-only | 101147 | 808259 |
| VoIP speaker-only | 101699 | 842103 |
| Downmix + analog controls + capture example | 112879 | 940835 |

All entries compiled, linked and produced firmware. Values are linker/build
measurements, not runtime free heap. S3 baseline and fixed-default profiles use
the same application and dependency choices; source paths identify the frozen
baseline versus candidate. The final maintained firmware adds 712 flash bytes
and no static RAM in this comparison.

For a 512-output-frame converter with two microphones, one reference and stride
four, host allocation accounting measured scratch changes of 3072 to 0 bytes
for same-rate 16-bit input, 8192 to 4096 for same-rate 32-bit input, and 27648 to
24576 for 32-bit 48-to-16 kHz conversion. The 16-bit 48-to-16 case stays at 12288.
Published API heap samples after the two matching default-profile cycles were:

| API metric | Original baseline | Final maintained firmware |
| --- | ---: | ---: |
| Free Heap | 111139 | 110807 |
| Minimum Free Heap since boot | 24044 | 23824 |
| Largest Free Block | 63488 | 63488 |

These sensors have their own sampling intervals; timestamps are retained in the
manifest. They are observations from this run, not a continuous allocator trace,
a leak diagnosis or a performance percentage. Optional DSP profiles perform
different processing and are not presented as equivalent memory benchmarks.

Vendor DSP allocations are excluded from these scratch figures. This is not a
claim of an equivalent total-heap saving on every board.

## Physical WS3 checks and remaining limits

The reachable WS3 without LCD was tested with master/media at one percent.
Each current profile completed two real SIP/media cycles:

- single-microphone AEC;
- averaged dual-microphone AEC;
- dual-microphone AFE;
- averaged mono-input AFE.

Cycles include bidirectional SIP/RTP, HTTP media at 48 kHz, announcements at 48
and 16 kHz, independent player stops, playback restart, remote/local hangup and
cleanup. The older bench asset named `music-48k.flac` is a synthetic 733-Hz tone,
as checked during the later acoustic measurements. It is a media-flow fixture,
not evidence of music quality or acoustic cancellation. PCM capture runs during
simultaneous playback and calls. Captures preserve
two physical 48-kHz channels where selected, and the expected one/two processor
input channels at 16 kHz. Recorded capture positions have no queue gaps in these
windows; observed RTP sequences are continuous. Analog requests were applied
and restored. The active mode reported by these bench profiles was
`fd_high_perf`; this does not separately qualify every AEC/AFE mode.

The baseline was also tested on the same board. The maintained FULL AFE firmware
is restored after diagnostics, with its normal defaults and without capture/PGA
bench entities. The final pass repeats ordinary calls and playback on that
firmware. Image identity, final state and cleanup are recorded in local evidence.

The existing bench initially rejected a saved-off Wake Word setting on the AEC
profile. Its setup now permits that initial preference, enables MWW through the
normal control for measured segments and restores it. The final maintained
profile returns to the pre-task enabled setting. This was a bench precondition
issue, not a firmware fix. A separate upload attempt used a generated path while
another build replaced it; no image was uploaded in that attempt. Subsequent
uploads use immutable copied images and verify the running build before commands.

No calibrated acoustic echo reduction, natural wake-word accuracy or audible
quality claim is made at one-percent volume. Host fault injection does not prove
an I2S fault was induced on silicon. New P4 physical camera/display testing,
LCD-S3 and Spotpear qualification, H.264, long soak, hostname/Replaces and
arbitrary PBX interoperability were not performed in this task. The LCD device
was unreachable and was not awakened. No claim is made about the previous
109-hour Spotpear incident.

### Authorized acoustic comparison at audible volume, 2026-10-22

The user subsequently authorized media at 100 percent and codec/master volume
at 50 percent. Three identified images were measured on the same WS3 without
LCD: maintained dual-mic AFE, single-mic standalone AEC and averaged dual-mic
standalone AEC. No DSP implementation was changed for these measurements.

The physical board itself played a 48-kHz stereo speech fixture and a real
SoundHelix music excerpt. The SIP peer sent continuous silent RTP and recorded
the processed microphone return at 16 kHz. Each fixture was repeated with AEC
off/on/on/off. Analysis retains the initial samples separately and excludes the
first six seconds for steady-level comparisons. Source correlation and spectral
coherence distinguish playback leakage from changing room noise.

All three comparisons show reduced playback leakage with cancellation enabled.
For the averaged-microphone AEC profile, the median returned level during music
was -59.3 dBFS with AEC off and -84.2 dBFS with it on, a 24.9-dB difference in
total returned level. This is not calibrated ERLE or a claim that all of that
power is echo. Room noise and post-processor gain affect these levels. For
example, AFE's changing room-noise floor makes its total RMS a poor measure of
the remaining playback component. The reference correlation/coherence records
are retained rather than ranking profiles using total RMS alone.

The completed windows had zero missing, duplicate or reordered RTP sequences
and zero digitally clipped return samples. The two standalone AEC profiles
also captured physical microphone, processor input/output and final output
during music. The effective reference is present when the processor runs.
Those saved taps had no queue-position gaps or digitally clipped samples.
PCAPs passed `capinfos`, with zero kernel capture drops reported.

One initial acquisition stopped because the bench incorrectly required a
reference tap while standalone AEC was bypassed. The existing documented
contract and production call site explain its absence. Cleanup completed;
the corrected bench requires four taps in bypass and five when active. The
failed run is retained separately and is not counted as completed qualification.

The new evidence is in `test_captures/aec-acoustic-20261022/`, with exact image
hashes/build times, source/fixture provenance, WAVs, PCAPs and per-window results.
These measurements do not qualify double-talk, near-end speech preservation,
wake-word accuracy, cold-boot convergence, LCD hardware or other rooms. The
averaged-input AFE profile was not acoustically measured in this session.

At the user's request, the averaged dual-microphone standalone AEC image remains
installed for a manual VoIP test. Its build identity is
`2026-10-21 23:43:13 +0000`; AEC is enabled in `fd_high_perf` mode, codec/master
volume is 50 percent and media volume is 100 percent. The bench released its
call and playback before handing the board back. The earlier maintained-image
restoration above describes the preceding low-volume session, not this final
user-selected state.

## CI and retained evidence

New native regressions are collected by the existing component pytest jobs.
ESP VoIP CI now also compiles microphone-only and speaker-only firmware. Audio
Stack CI compiles the opt-in example containing downmix, PGA and capture
simultaneously and lints the receiver tool. Required dependencies fail rather
than silently skip. The results above are local evidence; publishing the
checkpoint triggers separate GitHub runs and does not retroactively turn these
local results into CI results.

The gitignored local evidence directory is
`test_captures/esp-consolidation-20261021/`. It retains the original audit input,
separate before/after JUnit and logs, additional fixture/setup failures,
configuration/image/source manifests, PCM WAVs, valid PCAPs, hardware summaries
and `RESULTS.json`. Prototype evidence is labeled separately from final results.
