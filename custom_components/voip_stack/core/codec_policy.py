"""Codec allowlists applied to one SIP leg, without changing RTP mappings."""

from dataclasses import dataclass

AUDIO_CODECS = ("OPUS", "G722", "PCMA", "PCMU", "L16", "L24", "PCM")
VIDEO_CODECS = ("H264", "VP8", "JPEG", "H263", "H263P", "H265")


def normalize_codecs(value, supported, *, allow_empty=False):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("Select codecs from the supported list")
    result = tuple(dict.fromkeys(item.strip().upper() for item in value))
    if (not result and not allow_empty) or any(item not in supported for item in result):
        raise ValueError("Select at least one supported audio codec" if not allow_empty else "Select supported video codecs")
    return result


@dataclass(frozen=True, slots=True)
class CodecPolicy:
    """None preserves existing capabilities; an empty video list disables video."""

    audio: tuple[str, ...] | None = None
    video: tuple[str, ...] | None = None

    def audio_formats(self, formats):
        return tuple(item for item in formats if self.audio is None or item.encoding.upper() in self.audio)

    def video_formats(self, formats):
        return tuple(item for item in formats if self.video is None or item.encoding.upper() in self.video)

    @classmethod
    def from_config(cls, config):
        return cls(
            normalize_codecs(config.get("trunk_audio_codecs", AUDIO_CODECS), AUDIO_CODECS),
            normalize_codecs(config.get("trunk_video_codecs", VIDEO_CODECS), VIDEO_CODECS, allow_empty=True),
        )
