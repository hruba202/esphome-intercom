# Sendspin compatibility

This optional component is loaded by the full media profiles, alongside the
native ESPHome mixer and speaker-source adapters. Audio-only VoIP profiles do
not load it.

For the Sendspin C++ 0.9.3 dependency selected by ESPHome 2026.10.0, it retains:

- Scoped restoration of the creating task's pthread configuration and explicit
  failure handling before a worker is created. Upstream already supplies the
  correct byte-access capability for PSRAM stacks.
- Decoded-prefix trimming for a partially late chunk instead of discarding its
  entire encoded payload before decoding.

The adapter compiles one replacement translation unit for each enabled role.
It does not create another player, audio queue or synchronization clock, and
leaves disabled artwork/visualizer roles excluded. The dependency's source is
not modified in the package cache. Source checks stop the build if the relevant
upstream implementation changes, so a dependency update requires review rather
than silently applying an obsolete patch.

The mixer retains upstream playback counters and task lifecycle. The
speaker-source player reserves pending frames before its blocking speaker
write, allowing completion callbacks during that write to be counted correctly.
Both retain the standard ESPHome speaker interfaces. No callback mutex or
separate contribution queue is added by this adapter.

## Supported source generation

The CMake adapter accepts the audited 0.9.3/0.9.5 shared-inbound-ring path.
Unknown source patterns fail configuration. The old 0.7 encoded-ring matcher
and its version-selection branch are removed. Tests exercise the CMake adapter
against pinned modern revisions and the worker-configuration lifetime.

The native ESPHome 2026.10 hub owns discovery, encryption, pairing and persisted
identity. Its dependency already numbers stream starts, so no legacy startup
backport is installed. Pair using the native device controls and Music Assistant
2.10.7 or newer, following the [pairing guide](../../../packages/README.md#pair-sendspin-with-music-assistant).
This adapter does not create another stream owner or change the wire protocol.
