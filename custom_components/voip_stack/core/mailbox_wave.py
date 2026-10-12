"""Finalize a format-changing recording as one canonical mailbox WAV."""

import os
import tempfile
import wave

from .audio_format import AudioFormat
from .audio_pcm import PcmFrameConverter


def wave_format(message):
    segments = message["segments"]
    rate = max(item["sample_rate"] for item in segments)
    width = max(item["sample_width"] for item in segments)
    channels = max(item["channels"] for item in segments)
    destination = AudioFormat(rate, {2: "s16le", 3: "s24le", 4: "s32le"}[width], channels, 20)
    total_frames = max(round(item["start"] * rate) + round(item["frames"] * rate / item["sample_rate"])
                       for item in segments)
    return destination, total_frames


def write_wave(root, message, path):
    """Preserve native quality, upsampling only when stored formats differ."""
    from pathlib import Path
    from .media_archive import ArchiveError, _segment_path

    destination, _total_frames = wave_format(message)
    rate, width, channels = destination.sample_rate, destination.container_bytes_per_sample, destination.channels
    stride = width * channels
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".voicemail-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            with wave.open(file, "wb") as output:
                output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
                written = 0
                for segment in message["segments"]:
                    if segment["channel"] != "caller":
                        raise ArchiveError("Select a voicemail message, not a conversation recording")
                    start = round(segment["start"] * rate)
                    while written < start:
                        count = min(start - written, destination.nominal_frame_samples)
                        output.writeframesraw(bytes(count * stride))
                        written += count
                    source = AudioFormat(segment["sample_rate"], {2: "s16le", 3: "s24le", 4: "s32le"}[segment["sample_width"]],
                                         segment["channels"], 20)
                    converter = PcmFrameConverter(source, destination)
                    remaining = round(segment["frames"] * rate / source.sample_rate)
                    with wave.open(str(_segment_path(root, message["id"], segment["file"])), "rb") as input_file:
                        while pcm := input_file.readframes(source.nominal_frame_samples):
                            pcm += bytes(source.nominal_frame_bytes - len(pcm))
                            for converted in converter.convert(pcm):
                                count = min(remaining, len(converted) // stride)
                                output.writeframesraw(converted[:count * stride])
                                remaining -= count
                                written += count
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        return {"file": path.name, "channel": "caller", "sample_rate": rate,
                "sample_width": width, "channels": channels, "frames": written, "start": 0.0}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
