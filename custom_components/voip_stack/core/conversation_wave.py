"""Finalize both recorded directions into one WAV with bounded working memory."""

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import wave

import numpy as np

from .audio_format import AudioFormat
from .audio_pcm import PcmFrameConverter, _decode_frame, _encode_frame
from .mailbox_wave import wave_format


def write_wave(root, message, path):
    """Mix the recorded timeline off-loop, retaining the highest native format."""
    from .media_archive import _segment_path

    destination, total = wave_format(message)
    rate = destination.sample_rate
    segments = sorted(message["segments"], key=lambda item: item["start"])
    temporary = None
    with ExitStack() as files:
        def converted(segment):
            source = AudioFormat(segment["sample_rate"],
                {2: "s16le", 3: "s24le", 4: "s32le"}[segment["sample_width"]], segment["channels"], 20)
            converter = PcmFrameConverter(source, destination)
            input_file = files.enter_context(wave.open(str(_segment_path(root, message["id"], segment["file"])), "rb"))
            while pcm := input_file.readframes(source.nominal_frame_samples):
                pcm += bytes(source.nominal_frame_bytes - len(pcm))
                for block in converter.convert(pcm):
                    yield _decode_frame(block, destination)

        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".voicemail-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                with wave.open(file, "wb") as output:
                    output.setparams((destination.channels, destination.container_bytes_per_sample, rate, 0, "NONE", "not compressed"))
                    active = []
                    next_segment = 0
                    position = 0
                    while position < total:
                        count = min(destination.nominal_frame_samples, total - position)
                        end = position + count
                        while next_segment < len(segments) and round(segments[next_segment]["start"] * rate) < end:
                            segment = segments[next_segment]
                            start = round(segment["start"] * rate)
                            active.append({"cursor": start, "end": start + round(segment["frames"] * rate / segment["sample_rate"]),
                                "frames": converted(segment), "pending": np.empty((destination.channels, 0))})
                            next_segment += 1
                        mixed = np.zeros((destination.channels, count))
                        for item in active:
                            while item["cursor"] < min(end, item["end"]):
                                if not item["pending"].shape[1]:
                                    item["pending"] = next(item["frames"], np.empty((destination.channels, 0)))
                                    if not item["pending"].shape[1]:
                                        item["cursor"] = item["end"]
                                        break
                                amount = min(end - item["cursor"], item["end"] - item["cursor"], item["pending"].shape[1])
                                offset = item["cursor"] - position
                                mixed[:, offset:offset + amount] += item["pending"][:, :amount]
                                item["pending"] = item["pending"][:, amount:]
                                item["cursor"] += amount
                        active = [item for item in active if item["cursor"] < item["end"]]
                        output.writeframesraw(_encode_frame(mixed, destination))
                        position = end
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            return {"file": path.name, "channel": "caller", "sample_rate": rate,
                "sample_width": destination.container_bytes_per_sample, "channels": destination.channels,
                "frames": total, "start": 0.0}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
