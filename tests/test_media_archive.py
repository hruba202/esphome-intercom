"""Real WAV persistence, quota ownership and interrupted-recording recovery."""

import asyncio
import importlib
import json
from pathlib import Path
import struct
import sys
import subprocess
from types import ModuleType
import wave

import pytest


@pytest.fixture
def module(monkeypatch):
    name = "_voip_archive_test"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/voip_stack")]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.core.media_archive")


@pytest.fixture
async def archive(tmp_path, module):
    store = module.MediaArchive(tmp_path / "private", asyncio.to_thread)
    await store.load()
    yield store
    await store.close()


async def test_recording_keeps_native_quality_and_requires_matching_mailbox(archive, module):
    fmt = module.AudioFormat(48000, "s16le", 2, 20)
    pcm = struct.pack("<1920h", *range(1920))
    writer = await archive.begin("kitchen", caller="Delivery")
    assert writer.offer(pcm, fmt, offset=0)
    entry = await writer.finish()
    path = archive.segment_path(entry["id"], "kitchen")
    assert path.stat().st_mode & 0o777 == 0o600
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48000, 2, 2)
        assert wav.readframes(wav.getnframes()) == pcm
    assert archive.counts("kitchen") == (1, 0)
    assert archive.list("bedroom") == []
    with pytest.raises(module.ArchiveError, match="belong"):
        archive.segment_path(entry["id"], "bedroom")
    await archive.mark_read(entry["id"], "kitchen")
    await archive.load()
    assert archive.counts("kitchen") == (0, 1)
    await archive.delete(entry["id"], "kitchen")
    assert not list(archive.root.iterdir())
    assert archive._reserved == archive._used == 0


async def test_conversation_directions_mix_without_downsampling_or_voicemail_counts(archive, module):
    import math
    import numpy as np
    writer = await archive.begin("kitchen", kind="recording")
    caller = struct.pack("<960h", *(int(4000 * math.sin(2 * math.pi * 500 * i / 48000)) for i in range(960)))
    callee = struct.pack("<320h", *(int(4000 * math.sin(2 * math.pi * 1000 * i / 16000)) for i in range(320)))
    assert writer.offer(caller, module.AudioFormat(48000, "s16le", 1, 20), offset=0)
    assert writer.offer(callee, module.AudioFormat(16000, "s16le", 1, 20), channel="callee", offset=0)
    assert writer.offer(caller, module.AudioFormat(48000, "s16le", 1, 20), offset=1)
    entry = await writer.finish()
    assert len(entry["segments"]) == 1
    assert archive.counts("kitchen") == (0, 0)
    assert archive.list("kitchen", kind="recording") == [entry]
    path = archive.segment_path(entry["id"], "kitchen")
    with wave.open(str(path), "rb") as wav:
        assert (wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (2, 48000, 48960)
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    spectrum = np.abs(np.fft.rfft(samples[:960].astype(float)))
    assert spectrum[10] > 1_000_000 and spectrum[20] > 1_000_000
    assert max(spectrum[35:]) < min(spectrum[10], spectrum[20]) / 10
    assert not np.any(samples[960:48000])
    assert samples[48000:].tobytes() == caller
    assert list(archive.root.glob("*.wav")) == [path]
    assert archive._reserved == 0


async def test_empty_calls_leave_no_message_or_reserved_quota(archive):
    writer = await archive.begin("kitchen")
    assert await writer.finish() is None
    assert not archive.list("kitchen")
    assert not list(archive.root.iterdir())
    assert not archive._active and archive._reserved == 0


async def test_message_is_one_wav_after_native_format_changes(archive, module):
    writer = await archive.begin("kitchen")
    assert writer.offer(struct.pack("<320h", *([1000] * 320)), module.AudioFormat(16000, frame_ms=20), offset=0)
    native = struct.pack("<1920i", *([500000, -500000] * 960))
    assert writer.offer(native, module.AudioFormat(48000, "s24le_in_s32", 2, 20), offset=0.04)
    entry = await writer.finish()
    path = archive.segment_path(entry["id"], "kitchen")
    assert path.stat().st_mode & 0o777 == 0o600
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (48000, 2, 3, 2880)
        data = wav.readframes(2880)
    assert data[960 * 6:1920 * 6] == bytes(960 * 6)
    assert data[1920 * 6:] == b"".join(native[index:index + 3] for index in range(0, len(native), 4))
    assert archive.message(entry["id"], "kitchen") == entry
    assert archive.counts("kitchen") == (1, 0)
    assert len(entry["segments"]) == 1
    assert list(archive.root.glob("*.wav")) == [path]
    assert archive._reserved == 0


async def test_short_message_uses_its_original_file_without_another_copy(archive, module):
    writer = await archive.begin("kitchen")
    pcm = b"\x01\x00" * 17
    assert writer.offer(pcm, module.AudioFormat(16000, frame_ms=20), offset=0)
    entry = await writer.finish()
    path = archive.segment_path(entry["id"], "kitchen")
    with wave.open(str(path), "rb") as wav:
        assert wav.readframes(100) == pcm
    assert list(archive.root.glob("*.wav")) == [path]
    assert path.name.endswith("-000.wav")
    assert archive.list("kitchen") == [entry]


@pytest.mark.parametrize("failure", ["quota", "commit"])
async def test_interrupted_finalization_recovers_one_message_without_duplicate_audio(archive, module, monkeypatch, failure):
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(320), module.AudioFormat(8000, frame_ms=20), offset=0)
    assert writer.offer(bytes(640), module.AudioFormat(16000, frame_ms=20), offset=1)
    original_commit = module._atomic_json
    if failure == "quota":
        archive.quota_bytes = module._METADATA_RESERVE + 960 + 32684 - 1
    else:
        def interrupted(path, data):
            if any(item["file"].endswith("-final.wav") for item in data["segments"]):
                raise OSError("Interrupted before the final manifest commit")
            return original_commit(path, data)
        monkeypatch.setattr(module, "_atomic_json", interrupted)
    with pytest.raises(module.ArchiveError, match="finalized"):
        await writer.finish()
    assert not archive._active and archive._reserved == 0
    assert not archive.list("kitchen")
    monkeypatch.setattr(module, "_atomic_json", original_commit)
    archive.quota_bytes = 1024 ** 2
    await archive.load()
    assert archive.recovery_errors == 0
    entries = archive.list("kitchen")
    assert len(entries) == len(list(archive.root.glob("*.wav"))) == 1
    assert len(entries[0]["segments"]) == 1
    with wave.open(str(archive.segment_path(entries[0]["id"], "kitchen")), "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnframes() == 16320


@pytest.mark.parametrize("operation", ["mark_read", "delete"])
async def test_cancelled_mailbox_write_finishes_disk_and_memory_together(archive, module, operation):
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(640), module.AudioFormat(16000, frame_ms=20))
    entry = await writer.finish()
    written, release = asyncio.Event(), asyncio.Event()
    original_io = archive.run_io

    async def io(function, *args):
        result = await original_io(function, *args)
        if function is module._atomic_json or function.__name__ == "remove":
            written.set()
            await release.wait()
        return result

    archive.run_io = io
    task = asyncio.create_task(getattr(archive, operation)(entry["id"], "kitchen"))
    try:
        await asyncio.wait_for(written.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        counts = archive.counts("kitchen")
        used = archive._used
        await archive.load()
        assert archive.counts("kitchen") == counts == ((0, 1) if operation == "mark_read" else (0, 0))
        # JSON true is one byte shorter than false. A conservative live quota
        # estimate is allowed; losing the deleted file's reservation is not.
        assert 0 <= used - archive._used <= (1 if operation == "mark_read" else 0)
        assert not archive._lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_finishing_one_writer_does_not_double_count_another_active_file(archive, module):
    archive.quota_bytes = 250000
    written = asyncio.Event()
    async def io(function, *args):
        result = await asyncio.to_thread(function, *args)
        if getattr(function, "__name__", "") == "write":
            written.set()
        return result
    archive.run_io = io
    first, second = await archive.begin("first"), await archive.begin("second")
    for _ in range(4):
        assert second.offer(b"\x01\x00" * 12500, module.AudioFormat(16000, "s16le", 1, 20))
    await asyncio.wait_for(written.wait(), 2)
    await asyncio.to_thread(lambda: [stream.flush() for _wav, stream in second.disk.open_files.values()])
    assert first.identity != second.identity
    assert sum(path.stat().st_size for path in archive.root.glob("*.wav")) >= 100000
    assert first.offer(b"\x02\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    assert await first.finish() is not None
    assert not second.task.done() and not second.error
    assert archive._used < 10000, (archive._used, archive._reserved)
    assert second.offer(b"\x03\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    await second.finish()
    assert archive._reserved == 0
    assert len(archive.list("first")) == len(archive.list("second")) == 1


async def test_cancelling_begin_removes_its_created_journal(archive, module):
    created, release = asyncio.Event(), asyncio.Event()
    async def io(function, *args):
        result = await asyncio.to_thread(function, *args)
        if function is module._DiskRecording:
            created.set()
            await release.wait()
        return result
    archive.run_io = io
    task = asyncio.create_task(archive.begin("kitchen"))
    await asyncio.wait_for(created.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not archive._active and archive._reserved == 0
    assert not list(archive.root.iterdir())


async def test_quota_refusal_and_invalid_chunks_leave_the_archive_reusable(archive, module):
    archive.quota_bytes = module._METADATA_RESERVE - 1
    with pytest.raises(module.ArchiveError, match="quota"):
        await archive.begin("kitchen")
    assert not archive.root.exists()
    archive.quota_bytes = module._METADATA_RESERVE + 32
    writer = await archive.begin("kitchen")
    assert not writer.offer(b"\x01\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    assert writer.error == "quota_exceeded"
    assert await writer.finish() is None
    archive.quota_bytes = 1024 ** 2
    writer = await archive.begin("kitchen")
    assert not writer.offer(bytes(module._MAX_PCM_CHUNK + 2), module.AudioFormat())
    assert await writer.finish() is None
    writer = await archive.begin("kitchen")
    assert writer.offer(b"\x01\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    assert await writer.finish() is not None
    assert not archive._active and archive._reserved == 0


async def test_executor_future_interface_is_supported(archive):
    from functools import partial

    loop = asyncio.get_running_loop()
    archive.run_io = lambda function, *args: loop.run_in_executor(None, partial(function, *args))
    writer = await archive.begin("kitchen")
    assert await writer.finish() is None
    assert not archive._active and archive._reserved == 0


async def test_process_crash_recovers_actual_wav_frames_from_the_journal(archive, module):
    identity = "a" * 32
    component = Path(__file__).parents[1] / "custom_components/voip_stack"
    script = r'''
import importlib, os, sys, types
from pathlib import Path
package = types.ModuleType("archive_crash")
package.__path__ = [sys.argv[1]]
sys.modules["archive_crash"] = package
module = importlib.import_module("archive_crash.core.media_archive")
disk = module._DiskRecording(Path(sys.argv[2]), {
    "id": "a" * 32, "mailbox_id": "kitchen", "kind": "voicemail", "caller": "Caller",
    "created_at": 1.0, "read": False, "recovered": False, "status": "recording", "segments": [],
})
fmt = module.AudioFormat(48000, "s16le", 1, 20)
disk.write([("caller", b"\x01\x00" * 960, fmt, 0)])
disk.write([("caller", b"\x02\x00" * 960, fmt, .02)])
for wav, stream in disk.open_files.values():
    stream.flush()
os._exit(0)
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(component), str(archive.root)],
                                     capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    await archive.load()
    entry = archive.list("kitchen")[0]
    assert entry["recovered"] and entry["status"] == "partial"
    with wave.open(str(archive.segment_path(identity, "kitchen")), "rb") as wav:
        assert wav.getnframes() == 1920
        assert wav.readframes(1920) == b"\x01\x00" * 960 + b"\x02\x00" * 960
    assert not list(archive.root.glob("*.pending.json"))
    assert archive.counts("kitchen") == (1, 0)


async def test_stale_journal_cannot_overwrite_a_committed_read_message(archive, module):
    writer = await archive.begin("kitchen")
    writer.offer(b"\x01\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    entry = await writer.finish()
    await archive.mark_read(entry["id"], "kitchen")
    pending = archive.root / f'{entry["id"]}.pending.json'
    pending.write_text(json.dumps({**entry, "read": False, "status": "recording"}))
    await archive.load()
    assert archive.counts("kitchen") == (0, 1)
    assert not archive.list("kitchen")[0]["recovered"]
    assert not pending.exists()


async def test_slow_writer_is_bounded_and_does_not_block_other_work(archive, module):
    entered, release = asyncio.Event(), asyncio.Event()
    async def io(function, *args):
        if getattr(function, "__name__", "") == "write":
            entered.set()
            await release.wait()
        return await asyncio.to_thread(function, *args)
    archive.run_io = io
    writer = await archive.begin("kitchen")
    fmt = module.AudioFormat(16000, "s16le", 1, 20)
    assert writer.offer(b"\x01\x00" * 320, fmt)
    await asyncio.wait_for(entered.wait(), 2)
    accepted = [writer.offer(b"\x02\x00" * 320, fmt) for _ in range(65)]
    assert sum(accepted) == module._QUEUE_FRAMES
    assert writer.failed.is_set() and writer.error == "writer_overrun"
    assert writer.queue.qsize() <= module._QUEUE_FRAMES
    release.set()
    entry = await writer.finish()
    assert entry["status"] == "partial" and entry["error"] == "writer_overrun"
    assert archive._reserved == 0 and not archive._active


async def test_disk_write_failure_releases_resources_and_allows_a_clean_recording(archive, module):
    fail = True
    async def io(function, *args):
        nonlocal fail
        if getattr(function, "__name__", "") == "write" and fail:
            fail = False
            raise OSError("synthetic disk write failure")
        return await asyncio.to_thread(function, *args)
    archive.run_io = io
    fmt = module.AudioFormat(16000, "s16le", 1, 20)
    writer = await archive.begin("kitchen")
    writer.offer(b"\x01\x00" * 320, fmt)
    assert await writer.finish() is None
    assert writer.error == "storage_error"
    assert not archive._active and archive._reserved == 0 and not writer.disk.open_files
    retry = await archive.begin("kitchen")
    assert retry.offer(b"\x02\x00" * 320, fmt)
    assert await retry.finish() is not None
    assert archive.counts("kitchen") == (1, 0)


async def test_metadata_commit_failure_preserves_recoverable_audio(archive, module, monkeypatch):
    original = module._atomic_json
    failed = False
    def save(path, data):
        nonlocal failed
        if not path.name.endswith(".pending.json") and not failed:
            failed = True
            raise OSError("synthetic metadata write failure")
        original(path, data)
    monkeypatch.setattr(module, "_atomic_json", save)
    writer = await archive.begin("kitchen")
    writer.offer(b"\x01\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    with pytest.raises(module.ArchiveError, match="finalized"):
        await writer.finish()
    assert not writer.disk.open_files and not archive._active and archive._reserved == 0
    await archive.load()
    assert archive.list("kitchen")[0]["recovered"]
    assert archive.counts("kitchen") == (1, 0)


async def test_symbolic_segment_cannot_expose_another_file(archive, module, tmp_path):
    writer = await archive.begin("kitchen")
    writer.offer(b"\x01\x00" * 320, module.AudioFormat(16000, "s16le", 1, 20))
    entry = await writer.finish()
    path = archive.segment_path(entry["id"], "kitchen")
    outside = tmp_path / "unrelated.txt"
    outside.write_text("unrelated private data")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(module.ArchiveError, match="symbolic"):
        archive.segment_path(entry["id"], "kitchen")
    await archive.load()
    assert not archive.list("kitchen") and archive.recovery_errors == 1
    assert outside.read_text() == "unrelated private data"


async def test_removing_mailbox_deletes_only_its_messages_and_is_idempotent(archive, module):
    fmt = module.AudioFormat(16000, frame_ms=20)
    for mailbox, kind in (("deleted", "voicemail"), ("other", "voicemail"), ("deleted", "recording")):
        writer = await archive.begin(mailbox, kind=kind)
        assert writer.offer(bytes(640), fmt)
        await writer.finish()
    await archive.remove_mailbox("deleted")
    await archive.remove_mailbox("deleted")
    assert not archive.list("deleted")
    assert len(archive.list("other")) == 1
    assert len(archive.list("deleted", kind="recording")) == 1
    await archive.load()
    assert not archive.list("deleted")
    assert len(archive.list("other")) == 1


async def test_conversation_quota_keeps_room_for_partial_final_file(archive, module):
    archive.quota_bytes = 72000
    writer = await archive.begin('kitchen', kind='recording')
    fmt = module.AudioFormat(16000, 's16le', 1, 20)
    pcm = struct.pack('<320h', *([1200] * 320))
    assert writer.offer(pcm, fmt, channel='caller', offset=0)
    assert writer.offer(pcm, fmt, channel='callee', offset=0)
    # Let the actual disk writer consume both directions before exhausting quota.
    async with asyncio.timeout(2):
        while not writer.disk.entry['segments'] or len(writer.disk.entry['segments']) < 2:
            await asyncio.sleep(.01)
    for index in range(1, 100):
        if not writer.offer(pcm, fmt, channel='caller', offset=index*.02):
            break
    assert writer.error == 'quota_exceeded'
    entry = await writer.finish()
    assert entry is not None and entry['status'] == 'partial'
    assert len(entry['segments']) == 1
    path = archive.segment_path(entry['id'], 'kitchen')
    with wave.open(str(path), 'rb') as wav:
        assert wav.getnframes() >= 320
    assert list(archive.root.glob('*.wav')) == [path]
    assert archive._used <= archive.quota_bytes and archive._reserved == 0


async def test_final_size_scan_failure_keeps_committed_audio_and_allows_retry(archive, module):
    archive.quota_bytes = 100000
    original_io = archive.run_io
    fail_scan = True

    async def io(function, *args):
        if function is module._audio_bytes and fail_scan:
            raise OSError("Temporary directory access failure")
        return await original_io(function, *args)

    archive.run_io = io
    writer = await archive.begin("kitchen")
    pcm = struct.pack("<320h", *range(320))
    assert writer.offer(pcm, module.AudioFormat(16000, frame_ms=20), offset=0)
    entry = await writer.finish()
    assert not archive._active
    assert archive._reserved == 0
    assert not writer.disk.open_files
    assert archive.message(entry["id"], "kitchen") == entry
    with wave.open(str(archive.segment_path(entry["id"], "kitchen")), "rb") as audio:
        assert audio.readframes(320) == pcm
    with pytest.raises(module.ArchiveError, match="storage"):
        await archive.begin("kitchen")
    assert not archive._active and archive._reserved == 0
    fail_scan = False
    retry = await archive.begin("kitchen")
    assert retry.offer(pcm, module.AudioFormat(16000, frame_ms=20), offset=0)
    assert await retry.finish() is not None
    await archive.load()
    assert archive.counts("kitchen") == (2, 0)
    assert archive._used == module._audio_bytes(archive.root)
    assert archive._reserved == 0


async def test_delete_scan_failure_keeps_deletion_committed_and_recovers_quota(archive, module):
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(640), module.AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    original_io = archive.run_io

    async def io(function, *args):
        if function is module._audio_bytes:
            raise OSError("Temporary directory access failure")
        return await original_io(function, *args)

    archive.run_io = io
    try:
        await archive.delete(message["id"], "kitchen")
        assert archive.counts("kitchen") == (0, 0)
        assert not list(archive.root.iterdir())
        with pytest.raises(module.ArchiveError, match="storage"):
            await archive.begin("kitchen")
    finally:
        archive.run_io = original_io
    retry = await archive.begin("kitchen")
    assert await retry.finish() is None
    assert archive._used == archive._reserved == 0
    assert not archive._active
