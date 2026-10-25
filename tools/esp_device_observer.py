"""Domain-aware ESPHome API identities for laboratory state recorders.

The integer key is only unique inside a component domain and subdevice. An
unnamed binary sensor and media player may legitimately share it.
"""
from typing import Any


def entity_identity(model: Any) -> tuple[str, int, int]:
    kind = type(model).__name__
    for suffix in ('EntityState', 'State', 'Info'):
        if kind.endswith(suffix):
            kind = kind[:-len(suffix)]
            break
    return kind, getattr(model, 'device_id', 0), model.key


def entity_index(entities):
    return {entity_identity(entity): entity for entity in entities}


class DiagnosticReader:
    """Collect one complete v1 diagnostic report, with no polling or device state ownership."""

    def __init__(self, component: str):
        self.tag = f'[{component}.diagnostics:'
        self.pending = None
        self.latest = None
        self.sequence = 0

    def feed(self, message: str):
        import re
        message = re.sub(r'\x1b\[[0-9;]*m', '', message)
        if self.tag not in message:
            return
        if 'BEGIN v=1 ' in message:
            self.pending = {}
        if self.pending is None:
            return
        # VoIP video repeats audio's packet counters and active flag.
        prefix = 'video_' if self.tag == '[voip_stack.diagnostics:' and re.search(r'\]:\s*video:\s', message) else ''
        self.pending.update((prefix + key, value) for key, value in re.findall(r'\b([a-z0-9_]+)=([^\s]+)', message))
        if 'END v=1 ' in message:
            self.latest, self.pending = self.pending, None
            self.sequence += 1

class AudioDiagnosticReader(DiagnosticReader):
    def __init__(self):
        super().__init__('audio_stack')

    def output_is_quiescent(self):
        report = self.latest or {}
        return all(report.get(key) == value for key, value in {
            'failed': 'NO', 'transition': 'NO', 'speaker': 'NO',
            'i2s_error': 'NO', 'speaker_queued_bytes': '0', 'pending_tx': '0',
        }.items())
