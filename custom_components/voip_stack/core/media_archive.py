"""Private, bounded WAV storage shared by mailboxes and call recordings.

SIP sessions own their recording handles. This module only owns queued disk
writes, quota reservations and persisted media, with no signaling or audio
decoder of its own. Filesystem operations run through the supplied executor.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import struct
import time
from uuid import uuid4
import wave

from .audio_format import AudioFormat, PcmFormat
from ..session_cleanup import async_wait_for_cleanup

_IDENTITY = re.compile(r"^[0-9a-f]{32}$")
_METADATA_RESERVE = 65536
_MAX_SEGMENTS = 128
_QUEUE_FRAMES = 64
_MAX_PCM_CHUNK = 32768


class ArchiveError(RuntimeError):
    """A recording cannot be created, finalized or accessed."""


def _atomic_json(path: Path, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > _METADATA_RESERVE - 8192:
        raise ArchiveError("Recording metadata exceeds its bound")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _segment_path(root: Path, identity: str, name: str) -> Path:
    if not _IDENTITY.fullmatch(identity) or not re.fullmatch(re.escape(identity) + r"-\d{3}\.wav", name):
        raise ArchiveError("Invalid recording segment")
    path = root / name
    if path.is_symlink():
        raise ArchiveError("Recording segments cannot be symbolic links")
    return path


def _validate_entry(data: dict, identity: str) -> None:
    if (not isinstance(data, dict) or not _IDENTITY.fullmatch(identity) or data.get("id") != identity
            or data.get("kind") not in {"voicemail", "recording"}
            or not isinstance(data.get("mailbox_id"), str)
            or not 1 <= len(data["mailbox_id"]) <= 255
            or not isinstance(data.get("segments"), list)
            or len(data["segments"]) > _MAX_SEGMENTS):
        raise ArchiveError("Invalid recording metadata")
    if not isinstance(data.get("read"), bool) or not math.isfinite(float(data.get("created_at", -1))):
        raise ArchiveError("Invalid recording state")
    for segment in data["segments"]:
        if (not isinstance(segment, dict) or segment.get("channel") not in {"caller", "callee"}
                or segment.get("sample_width") not in {2, 3, 4}
                or not isinstance(segment.get("frames"), int) or segment["frames"] < 0
                or not math.isfinite(float(segment.get("start", -1))) or float(segment.get("start", -1)) < 0):
            raise ArchiveError("Invalid recording audio format")
        AudioFormat(segment["sample_rate"], {2: "s16le", 3: "s24le", 4: "s32le"}[segment["sample_width"]],
                    segment["channels"], 20)


def _read_entry(path, identity):
    if path.is_symlink() or path.stat().st_size > _METADATA_RESERVE:
        raise ArchiveError("Invalid recording metadata")
    data = json.loads(path.read_text())
    _validate_entry(data, identity)
    return data


def _audio_bytes(root: Path, active: tuple[str, ...] = ()) -> int:
    return sum(path.stat().st_size for path in root.iterdir()
               if path.is_file() and not path.is_symlink() and path.name[:32] not in active) if root.exists() else 0


def _load(root: Path) -> tuple[dict, int, int]:
    entries, errors = {}, 0
    if not root.exists():
        return entries, 0, 0
    for pending in root.glob("*.pending.json"):
        identity = pending.name.removesuffix(".pending.json")
        try:
            committed = root / f"{identity}.json"
            if committed.exists():
                final = _read_entry(committed, identity)
                if (final.get("status") in {"complete", "partial"} and final["segments"]
                        and all(_segment_path(root, identity, item["file"]).is_file() for item in final["segments"])):
                    pending.unlink()
                    continue
            data = _read_entry(pending, identity)
            segments = []
            for segment in data["segments"]:
                path = _segment_path(root, identity, segment["file"])
                if not path.exists() or path.stat().st_size == 0:
                    path.unlink(missing_ok=True)
                    continue
                with path.open("r+b") as stream:
                    header = stream.read(44)
                    if len(header) != 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE" or header[36:40] != b"data":
                        raise ArchiveError("Invalid interrupted WAV header")
                    stride = int(segment["channels"]) * int(segment["sample_width"])
                    if stride not in {2, 3, 4, 6, 8}:
                        raise ArchiveError("Invalid interrupted WAV format")
                    size = (path.stat().st_size - 44) // stride * stride
                    stream.truncate(44 + size)
                    stream.seek(4)
                    stream.write(struct.pack("<I", 36 + size))
                    stream.seek(40)
                    stream.write(struct.pack("<I", size))
                if size:
                    segment["frames"] = size // stride
                    segments.append(segment)
                else:
                    path.unlink(missing_ok=True)
            if segments:
                data.update(segments=segments, recovered=True, status="partial", error="interrupted")
                _atomic_json(root / f"{identity}.json", data)
            pending.unlink()
        except (OSError, ValueError, KeyError, TypeError, ArchiveError):
            errors += 1
    for metadata in root.glob("*.json"):
        identity = metadata.stem
        if not _IDENTITY.fullmatch(identity):
            continue
        try:
            data = _read_entry(metadata, identity)
            if not data["segments"]:
                raise ArchiveError("Empty recording metadata")
            for segment in data["segments"]:
                if not _segment_path(root, identity, segment["file"]).is_file():
                    raise ArchiveError("Missing recording segment")
            entries[identity] = data
        except (OSError, ValueError, KeyError, TypeError, ArchiveError):
            errors += 1
    return entries, _audio_bytes(root), errors


class _DiskRecording:
    def __init__(self, root: Path, entry: dict):
        self.root, self.entry = root, entry
        self.open_files = {}
        self.current = {}
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pending = root / f'{entry["id"]}.pending.json'
        _atomic_json(self.pending, entry)

    def _close_channel(self, channel):
        pair = self.open_files.pop(channel, None)
        if pair is not None:
            wav, stream = pair
            try:
                wav.close()
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                stream.close()

    def write(self, frames):
        for channel, pcm, fmt, offset in frames:
            width = 3 if fmt.pcm_format is PcmFormat.S24LE_IN_S32 else fmt.container_bytes_per_sample
            if fmt.pcm_format is PcmFormat.S24LE_IN_S32:
                pcm = b"".join(pcm[index:index + 3] for index in range(0, len(pcm), 4))
            previous = self.current.get(channel)
            end = previous["start"] + previous["frames"] / previous["sample_rate"] if previous else 0.0
            if (previous is None or (previous["sample_rate"], previous["sample_width"], previous["channels"])
                    != (fmt.sample_rate, width, fmt.channels) or offset > end + 0.1):
                self._close_channel(channel)
                if len(self.entry["segments"]) >= _MAX_SEGMENTS:
                    raise ArchiveError("Too many recording format changes or gaps")
                name = f'{self.entry["id"]}-{len(self.entry["segments"]):03d}.wav'
                stream = os.fdopen(os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
                wav = wave.open(stream, "wb")
                wav.setnchannels(fmt.channels)
                wav.setsampwidth(width)
                wav.setframerate(fmt.sample_rate)
                self.open_files[channel] = (wav, stream)
                previous = {"file": name, "channel": channel, "sample_rate": fmt.sample_rate,
                            "sample_width": width, "channels": fmt.channels, "frames": 0,
                            "start": max(offset, end)}
                self.entry["segments"].append(previous)
                self.current[channel] = previous
                _atomic_json(self.pending, self.entry)
            self.open_files[channel][0].writeframesraw(pcm)
            previous["frames"] += len(pcm) // (width * fmt.channels)

    def finish(self, error=""):
        try:
            for channel in tuple(self.open_files):
                self._close_channel(channel)
        finally:
            for _wav, stream in self.open_files.values():
                stream.close()
            self.open_files.clear()
        for segment in self.entry["segments"]:
            if not segment["frames"]:
                _segment_path(self.root, self.entry["id"], segment["file"]).unlink(missing_ok=True)
        self.entry["segments"] = [segment for segment in self.entry["segments"] if segment["frames"]]
        if self.entry["segments"]:
            self.entry.update(status="partial" if error else "complete", error=error)
            _atomic_json(self.root / f'{self.entry["id"]}.json', self.entry)
            result = self.entry
        else:
            for path in self.root.glob(f'{self.entry["id"]}-*.wav'):
                path.unlink()
            result = None
        self.pending.unlink(missing_ok=True)
        return result


class ArchiveWriter:
    """One bounded recording sink. Offering PCM never waits for disk I/O."""

    def __init__(self, store, disk, identity):
        self.store, self.disk, self.identity = store, disk, identity
        self.queue = asyncio.Queue(maxsize=_QUEUE_FRAMES)
        self.failed = asyncio.Event()
        self.error = ""
        self.reserved = _METADATA_RESERVE
        self.started = time.monotonic()
        self.accepting = True
        self.result = None
        self.task = asyncio.create_task(self._run(), name=f"voip-archive-{identity}")
        self._finish_task = None

    def _fail(self, reason):
        self.error = reason
        self.accepting = False
        self.failed.set()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def offer(self, pcm: bytes, fmt: AudioFormat, *, channel="caller", offset=None) -> bool:
        if not self.accepting:
            return False
        offset = time.monotonic() - self.started if offset is None else float(offset)
        if (channel not in {"caller", "callee"} or not pcm or len(pcm) > _MAX_PCM_CHUNK
                or len(pcm) % (fmt.channels * fmt.container_bytes_per_sample)
                or not math.isfinite(offset) or offset < 0):
            self._fail("invalid_audio")
            return False
        if not self.store._reserve(len(pcm)):
            self._fail("quota_exceeded")
            return False
        self.reserved += len(pcm)
        try:
            self.queue.put_nowait((channel, bytes(pcm), fmt, offset))
        except asyncio.QueueFull:
            self.store._reserved -= len(pcm)
            self.reserved -= len(pcm)
            self._fail("writer_overrun")
            return False
        return True

    async def _run(self):
        try:
            while not self.error:
                first = await self.queue.get()
                if first is None:
                    break
                batch = [first]
                last = False
                while len(batch) < 32 and not self.queue.empty():
                    item = self.queue.get_nowait()
                    if item is None:
                        last = True
                        break
                    batch.append(item)
                await self.store.run_io(self.disk.write, batch)
                if last:
                    break
        except Exception:
            self._fail("storage_error")
        finally:
            self.accepting = False
            while not self.queue.empty():
                self.queue.get_nowait()
            try:
                self.result = await self.store.run_io(self.disk.finish, self.error)
            except Exception:
                self._fail("finalization_failed")
            await self.store._finished(self)

    async def finish(self):
        if self._finish_task is None:
            self.accepting = False
            async def finish():
                if not self.task.done():
                    await self.queue.put(None)
                await self.task
                if self.error == "finalization_failed":
                    raise ArchiveError("Recording could not be finalized")
                return deepcopy(self.result)
            self._finish_task = asyncio.create_task(finish(), name=f"voip-archive-close-{self.identity}")
        return await async_wait_for_cleanup(self._finish_task)


class MediaArchive:
    def __init__(self, root: Path, run_io, *, quota_bytes: int = 1024 ** 3):
        self.root, self.run_io = Path(root), run_io
        self.quota_bytes = int(quota_bytes)
        self._entries, self._active = {}, {}
        self._used = self._reserved = self.recovery_errors = 0
        self._lock = asyncio.Lock()

    async def load(self):
        async with self._lock:
            if self._active:
                raise ArchiveError("Cannot reload an active archive")
            async def read():
                return await self.run_io(_load, self.root)
            loading = asyncio.create_task(read(), name="voip-archive-load")
            self._entries, self._used, self.recovery_errors = await async_wait_for_cleanup(loading)

    def _reserve(self, count):
        if self._used + self._reserved + count > self.quota_bytes:
            return False
        self._reserved += count
        return True

    async def begin(self, mailbox_id: str, *, kind="voicemail", caller="") -> ArchiveWriter:
        if kind not in {"voicemail", "recording"} or not 1 <= len(mailbox_id) <= 255:
            raise ArchiveError("Invalid recording destination")
        async with self._lock:
            if not self._reserve(_METADATA_RESERVE):
                raise ArchiveError("Media archive quota exceeded")
            identity = uuid4().hex
            entry = {"id": identity, "mailbox_id": mailbox_id, "kind": kind,
                     "caller": str(caller)[:255], "created_at": time.time(), "read": False,
                     "recovered": False, "status": "recording", "error": "", "segments": []}
            async def create():
                return await self.run_io(_DiskRecording, self.root, entry)
            try:
                creation = asyncio.create_task(create(), name=f"voip-archive-create-{identity}")
                disk = await asyncio.shield(creation)
            except BaseException:
                async def rollback():
                    try:
                        created = await creation
                        await self.run_io(created.finish)
                    finally:
                        self._reserved -= _METADATA_RESERVE
                cleanup = asyncio.create_task(rollback(), name=f"voip-archive-create-rollback-{identity}")
                await async_wait_for_cleanup(cleanup)
                raise
            writer = ArchiveWriter(self, disk, identity)
            self._active[identity] = writer
            return writer

    async def _finished(self, writer):
        async with self._lock:
            used = await self.run_io(_audio_bytes, self.root, tuple(identity for identity in self._active if identity != writer.identity))
            self._active.pop(writer.identity, None)
            self._reserved -= writer.reserved
            self._used = used
            if writer.result is not None:
                self._entries[writer.identity] = deepcopy(writer.result)

    def list(self, mailbox_id: str, *, kind="voicemail", offset=0, limit=None) -> list[dict]:
        entries = sorted((entry for entry in self._entries.values()
                          if entry["mailbox_id"] == mailbox_id and entry["kind"] == kind),
                         key=lambda entry: entry["created_at"], reverse=True)
        return deepcopy(entries[offset:None if limit is None else offset + limit])

    def message(self, identity: str, mailbox_id: str) -> dict:
        return deepcopy(self._require(identity, mailbox_id))

    def counts(self, mailbox_id: str) -> tuple[int, int]:
        entries = [entry for entry in self._entries.values() if entry["mailbox_id"] == mailbox_id and entry["kind"] == "voicemail"]
        return sum(not entry["read"] for entry in entries), sum(bool(entry["read"]) for entry in entries)

    async def mark_read(self, identity: str, mailbox_id: str):
        async with self._lock:
            entry = deepcopy(self._require(identity, mailbox_id))
            entry["read"] = True
            async def commit():
                await self.run_io(_atomic_json, self.root / f"{identity}.json", entry)
                self._entries[identity] = entry
            await async_wait_for_cleanup(asyncio.create_task(commit(), name="voip-archive-mark-read"))

    def _require(self, identity, mailbox_id):
        entry = self._entries.get(identity)
        if entry is None or entry["mailbox_id"] != mailbox_id:
            raise ArchiveError("Recording does not belong to this mailbox")
        return entry

    def segment_path(self, identity, mailbox_id, index=0):
        entry = self._require(identity, mailbox_id)
        if not 0 <= index < len(entry["segments"]):
            raise ArchiveError("Recording segment does not exist")
        return _segment_path(self.root, identity, entry["segments"][index]["file"])

    async def delete(self, identity: str, mailbox_id: str):
        async with self._lock:
            entry = self._require(identity, mailbox_id)
            def remove():
                for segment in entry["segments"]:
                    _segment_path(self.root, identity, segment["file"]).unlink(missing_ok=True)
                (self.root / f"{identity}.json").unlink(missing_ok=True)
            async def commit():
                await self.run_io(remove)
                self._entries.pop(identity)
                self._used = await self.run_io(_audio_bytes, self.root, tuple(self._active))
            await async_wait_for_cleanup(asyncio.create_task(commit(), name="voip-archive-delete"))

    async def remove_mailbox(self, mailbox_id: str):
        """Delete mailbox messages without touching separate call recordings."""
        for writer in tuple(self._active.values()):
            if writer.disk.entry["mailbox_id"] == mailbox_id and writer.disk.entry["kind"] == "voicemail":
                await writer.finish()
        for entry in tuple(self.list(mailbox_id)):
            await self.delete(entry["id"], mailbox_id)

    async def close(self):
        await asyncio.gather(*(writer.finish() for writer in tuple(self._active.values())), return_exceptions=True)
