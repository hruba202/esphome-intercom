# ESPHome audio fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/audio`.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/audio esphome/components/audio
```

## Local patch

The host simulator uses the shared audio format, transfer buffer and decoder
interfaces without ESP-IDF. The local fork permits `audio:` on the host,
skips IDF component registration there and provides the narrow non-ESP32
speaker and error-code adapters needed by `audio_transfer_buffer`.

The current development provider also selects esp-audio-libs 4.1.0 and
micro-flac 0.3.0. Stable ESPHome 2026.9.1 still selects 3.2.1 and 0.2.0;
upstream dev already selects the newer pair. The host support and dependency
selection are distinct reasons for retaining this provider. Re-run the diff
after every ESPHome update instead of treating the old baseline record as
proof of dependency parity.
