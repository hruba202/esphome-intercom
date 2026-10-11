# ESPHome sound actions with Audio Stack

Audio Stack exposes the ordinary ESPHome speaker interface. A media player,
mixer or resampler uses that interface in the same way as a native I2S speaker.
No separate VoIP action is required to play a local sound on the ESP.

For a maintained FULL profile, use its media player and announcement pipeline.
From Home Assistant, select the ESP media-player entity and call
`media_player.play_media` with `announce: true`. Use the normal HA media picker
for the file. This is local ESP playback, separate from sending audio into an
active telephone call with VoIP Stack's `play_media` action.

An ESPHome automation can use the standard action as well:

```yaml
- media_player.play_media:
    id: speaker_media_player
    media_url: "https://YOUR_MEDIA_SERVER/doorbell.flac"
    announcement: true
```

Replace the example address with a file the ESP can reach. The maintained
profile's decoder and configured sources determine supported file formats.
VoIP, announcements and music keep the profile's existing mixer priorities.

For custom firmware that already has raw PCM samples, `speaker.play`,
`speaker.finish`, `speaker.stop` and `speaker.is_playing` also work with an
`esp_audio_stack` speaker. PCM bytes must match that speaker's configured sample
rate, sample width, channel count and byte order. `speaker.play` does not decode
MP3, WAV headers or an HTTP URL. Avoid sending raw PCM directly to the hardware
speaker while a mixer owns it; use the appropriate mixer input instead.

## Qualification

A compile-only Spotpear FULL fixture includes the standard raw speaker actions
and the media-player announcement action alongside AFE, Micro Wake Word, VoIP,
Voice Assistant, the display and the existing media pipelines. The fixture is
not a firmware to install: it contains dummy Wi-Fi credentials. Compilation
checks action registration, generated C++ and linkage. Physical playback and
concurrency still require a device test; no new hardware result is inferred
from this compile.
