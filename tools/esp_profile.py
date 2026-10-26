#!/usr/bin/env python3
"""Host-side ESP32-S3 profiling. No firmware probes or halting PC profiler.

DEBUGPC samples are a distribution of observed PCs, not task CPU percentages.
PMU events are whole-core counters, not flash/PSRAM cache-miss measurements.
Only the qualified official OpenOCD register interface is used.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import AbstractContextManager
import hashlib
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import stat
import statistics
import subprocess
import tempfile
import time
import uuid


OPENOCD_REVISION = "b0edad625b5edc0a64d52a641a7bd02e3a56c37d"
OPENOCD_VERSION = "v0.12.0-esp32-20260831"
CORES = ("esp32s3.cpu0", "esp32s3.cpu1")
TAPS = ("esp32s3.tap0", "esp32s3.tap1")
PMU_EVENTS = {
    "instructions": (2, 0x8DFF),
    "data_stalls": (3, 0x01FE),
    "instruction_stalls": (4, 0x01FF),
    "waiti": (6, 0x0100),
}
PMU_REGISTERS = (0x20, 0x28, 0x29, 0x30, 0x31)
READ_REGISTERS = (*PMU_REGISTERS, 0x0F, 0x38, 0x39, 0x44)


class ProfileError(RuntimeError):
    """Invalid acquisition, identity, transport or input."""


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


class OpenOCD(AbstractContextManager):
    """Small, allow-listed Tcl transport; never calls profile/halt/resume/reset."""

    def __init__(self, host: str, port: int, timeout: float = 3, *, systemview=False):
        if not ipaddress.ip_address(host).is_loopback:
            raise ProfileError("OpenOCD must listen on loopback")
        lock_path = Path(tempfile.gettempdir()) / f"esp-profile-{os.getuid()}-{port}.lock"
        self.lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.socket = socket.create_connection((host, port), timeout=timeout)
            self.socket.settimeout(timeout)
        except BaseException:
            os.close(self.lock)
            raise
        self.pending = b""
        self.systemview = systemview
        self.poll_owned = False
        self.reply_in_flight = False

    def __exit__(self, *args):
        try:
            if self.poll_owned:
                if self.reply_in_flight:
                    raise ProfileError("Tcl reply is incomplete; polling restoration is unverified. "
                                       "No further command was sent on the desynchronized connection")
                self._request("capture {poll on}")
                if self._poll_state() != "on":
                    raise ProfileError("OpenOCD background polling restore was not verified")
        except (OSError, ProfileError) as error:
            if args[1] is None:
                raise
            # Preserve the original transport failure instead of consuming its
            # pending reply as a cleanup reply and masking the actual cause.
            args[1].cleanup_errors = [*getattr(args[1], "cleanup_errors", []), str(error)]
        finally:
            self.socket.close()
            os.close(self.lock)

    def _poll_state(self) -> str:
        reply = self._request("capture {poll}")
        match = re.match(r"background polling: (on|off)(?:\n|$)", reply)
        if match is None:
            raise ProfileError(f"Unrecognized OpenOCD polling state: {reply}")
        return match[1]

    def _claim_scans(self) -> None:
        if not self.poll_owned:
            if self._poll_state() != "on":
                raise ProfileError("OpenOCD polling is already disabled by another owner")
            # jim_command_dispatch invokes target timers before every C
            # command, including between scans in one Tcl request. Suspend
            # only that host-side polling; otherwise it can select the other
            # core and replace our NARSEL with BYPASS. DSR checks below still
            # verify the actual running state; the CPU is never halted.
            self.poll_owned = True
            self._request("capture {poll off}")
            if self._poll_state() != "off":
                raise ProfileError("Could not claim the OpenOCD scan sequence")

    def command(self, command: str) -> str:
        allowed = command == "version" or command in {f"{core} curstate" for core in CORES}
        if self.systemview:
            allowed |= bool(re.fullmatch(
                r"esp sysview (?:status|stop|start \{file://[^{}\\\x00-\x1f]+\} "
                r"\{file://[^{}\\\x00-\x1f]+\} 1 [0-9]+ -1)", command
            ))
            allowed |= command == "debug_level" or bool(re.fullmatch(
                r"echo \{ESP_PROFILE_SYSVIEW_[0-9a-f]{32}_(?:BOUND|BEGIN|END)\}", command))
        if not allowed:
            raise ProfileError("Command outside the non-halting register interface")
        # SystemView replies use the logger; version/curstate are Jim results.
        wire_command = (f"capture {{{command}}}"
                        if command.startswith("esp sysview ") or command == "debug_level" else command)
        previous_timeout = self.socket.gettimeout()
        if command.startswith("esp sysview "):
            # Native STOP already permits five seconds of drain (thirty at
            # debug verbosity), before additional JTAG reads/finalization.
            self.socket.settimeout(max(previous_timeout or 0, 60))
        try:
            return self._request(wire_command)
        finally:
            self.socket.settimeout(previous_timeout)

    def _request(self, wire_command: str) -> str:
        if self.reply_in_flight:
            raise ProfileError("An earlier Tcl reply is incomplete; do not reuse this connection")
        self.reply_in_flight = True
        self.socket.sendall(wire_command.encode("ascii") + b"\x1a")
        while b"\x1a" not in self.pending:
            data = self.socket.recv(65536)
            if not data:
                raise ProfileError("OpenOCD closed the Tcl connection")
            self.pending += data
            if len(self.pending) > 1024 * 1024:
                raise ProfileError("Oversized OpenOCD reply")
        reply, self.pending = self.pending.split(b"\x1a", 1)
        self.reply_in_flight = False
        return reply.decode("utf-8", errors="strict").strip()

    def read(self, core: int, register: int) -> int:
        return self._register(core, register)

    def write(self, core: int, register: int, value: int) -> None:
        self._register(core, register, value)

    def _register(self, core: int, register: int, value: int | None = None) -> int:
        if core not in (0, 1) or register not in READ_REGISTERS:
            raise ProfileError("Register outside the profiling allow-list")
        if value is not None and (register not in PMU_REGISTERS or not 0 <= value <= 0xFFFFFFFF):
            raise ProfileError("Only bounded PMU writes are allowed")
        self._claim_scans()
        # ESP32-S3 omits generic 'xtensa dm'. Use stock JTAG commands with
        # exactly the NAR protocol of xtensa_dm_queue_reg_read/write. NAR
        # numbers are not the APB offsets accepted by generic 'xtensa dm'.
        # One Tcl request keeps the address/data scans together; irscan
        # handles BYPASS for the other TAP. No CPU instruction is injected.
        tap = TAPS[core]
        address = (register << 1) | int(value is not None)
        reply = self._request(
            f"irscan {tap} 0x1c -endstate IDLE; "
            f"drscan {tap} 8 0x{address:x} -endstate IDLE; "
            f"drscan {tap} 32 0x{(value or 0):08x} -endstate IDLE"
        )
        if re.fullmatch(r"[0-9a-fA-F]{8}", reply) is None:
            raise ProfileError(f"Invalid JTAG NAR reply for CPU{core}: {reply}")
        return int(reply, 16)

    def running(self) -> None:
        for index, core in enumerate(CORES):
            if self.command(f"{core} curstate") != "running" or self.read(index, 0x44) & 0x10:
                raise ProfileError(f"{core} is not running; acquisition rejected")


def identity(elf: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    with elf.open("rb") as stream:
        header = stream.read(52)
    if len(header) < 52 or header[:6] != b"\x7fELF\x01\x01" or int.from_bytes(header[18:20], "little") != 94:
        raise ProfileError("An ESP32-S3 Xtensa ELF is required")
    if manifest.get("elf_sha256") != sha256(elf):
        raise ProfileError("ELF does not match the build manifest")
    for field in ("device_mac", "profile", "scenario_contract", "source_revisions", "firmware_sha256"):
        if not manifest.get(field):
            raise ProfileError(f"Missing build identity field: {field}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", manifest["firmware_sha256"]):
        raise ProfileError("A full firmware SHA256 is required")
    revisions = manifest["source_revisions"]
    if not isinstance(revisions, dict) or any(
        not isinstance(revision, dict) or
        not re.fullmatch(r"[0-9a-fA-F]{40}", revision.get("head", "")) or
        not re.fullmatch(r"[0-9a-fA-F]{64}", revision.get("tree_sha256", "")) or
        not isinstance(revision.get("dirty"), bool)
        for revision in revisions.values()
    ):
        raise ProfileError("Source identities require exact HEAD, content hash and dirty state")
    running = manifest.get("running_identity", {})
    if not running.get("evidence") or running.get("elf_sha256") != manifest["elf_sha256"]:
        raise ProfileError("Running firmware identity must be independently verified and recorded")
    evidence = Path(running["evidence"])
    if not evidence.is_file() or running.get("evidence_sha256") != sha256(evidence):
        raise ProfileError("Running firmware identity evidence is missing or has changed")
    return manifest


def validate_sysview_boot(manifest: dict) -> dict:
    """Admit saved external observations, without uploading or contacting API/JTAG.

    The boot log precedes IDF's unchecked mark-valid return. A later live app
    identity can show persistence across a reboot; old runtime pointers cannot.
    """
    def receipt(key):
        reference = manifest.get(key, {})
        path = Path(reference.get("evidence", ""))
        if not path.is_file() or reference.get("evidence_sha256") != sha256(path):
            raise ProfileError(f"SystemView {key} evidence is missing or has changed")
        return json.loads(path.read_text())

    def timestamp(value):
        try:
            stamp = float(value)
        except (TypeError, ValueError) as error:
            raise ProfileError("SystemView evidence has an invalid timestamp") from error
        if not math.isfinite(stamp) or stamp < 0:
            raise ProfileError("SystemView evidence has an invalid timestamp")
        return stamp

    upload = receipt("boot_confirmation")
    post = receipt("post_prefit_evidence")
    runtime = receipt("runtime_preflight_evidence")
    build = upload.get("build", {})
    expected_mac = manifest["device_mac"].upper()
    if (upload.get("status") != "uploaded_and_verified" or
            upload.get("target", {}).get("mac", "").upper() != expected_mac or
            upload.get("elf_sha256") != manifest["elf_sha256"] or
            upload.get("binary_sha256") != manifest["firmware_sha256"] or
            not build.get("build_time_str") or not build.get("esphome_version")):
        raise ProfileError("SystemView boot receipt does not match the target/build")

    def same_device(observed):
        return (observed.get("mac_address", "").upper() == expected_mac and
                observed.get("compilation_time") == build["build_time_str"] and
                observed.get("esphome_version") == build["esphome_version"])

    boot = upload.get("boot_confirmation", {})
    markers = [timestamp(row.get("at")) for row in boot.get("observed_log", [])
               if "Boot seems successful; resetting boot loop counter" in row.get("message", "")]
    if not markers or not same_device(boot.get("identity_after_boot_success", {})):
        raise ProfileError("SystemView requires a boot-success log and subsequent matching API identity")
    marker = max(markers)
    finished = timestamp(upload.get("finished_at", 0))
    post_at = timestamp(post.get("verified_at", 0))
    runtime_at = timestamp(runtime.get("verified_at", 0))
    if (finished < marker or post_at < finished or runtime_at < marker or runtime_at > post_at or
            not same_device(post.get("identity", {})) or
            runtime.get("elf_sha256") != manifest["elf_sha256"] or
            runtime.get("live_app_elf_sha256") != manifest["elf_sha256"]):
        raise ProfileError("SystemView post-preflight API/app identity or observation order is invalid")

    def boot_anchor(observation):
        # API subscriptions replay the last Uptime sensor value. Use the
        # requested diagnostic snapshot, whose uptime is sampled on demand.
        if "uptime_ms" not in observation.get("audio", {}):
            raise ProfileError("SystemView needs a fresh diagnostic uptime, not a cached Uptime sensor")
        uptime = timestamp(observation["audio"]["uptime_ms"]) / 1000
        observed_at = timestamp(observation.get("verified_at", 0))
        if uptime > observed_at:
            raise ProfileError("SystemView diagnostic uptime is inconsistent with its receipt")
        return observed_at - uptime

    before = upload.get("after", {})
    if not same_device(before.get("identity", {})):
        raise ProfileError("SystemView initial API identity is missing or inconsistent")
    initial_anchor, post_anchor = boot_anchor(before), boot_anchor(post)
    # Reports span several deferred loop turns. Five seconds is a declared
    # comparison tolerance, not proof of an exact host/device clock mapping.
    tolerance = 5
    rebooted = abs(post_anchor - initial_anchor) > tolerance
    if rebooted and runtime_at < post_anchor:
        raise ProfileError("SystemView runtime map predates the reboot; reacquire its app identity and pointers")
    return {"boot_marker_observed_at": marker, "boot_anchor_tolerance_seconds": tolerance,
            "reboot_after_confirmation": rebooted, "runtime_identity_observed_at": runtime_at,
            "uptime_source": "requested_audio_diagnostic", "partition_state_read_directly": False}


def sample_pcs(client, seconds: float, hz: float, emit, *, clock=time, seed=0) -> dict:
    if not 0 < seconds <= 600 or not 1 <= hz <= 200:
        raise ProfileError("PC capture requires 0 < seconds <= 600 and 1 <= Hz/core <= 200")
    client.running()
    rng = random.Random(seed)
    started = clock.monotonic()
    deadline = started + seconds
    next_sample = started
    next_guard = started + 1
    count = Counter()
    missed_slots = 0
    while clock.monotonic() < deadline:
        now = clock.monotonic()
        if now < next_sample:
            clock.sleep(min(next_sample - now, max(0, deadline - now)))
            continue
        if now >= next_guard:
            client.running()
            next_guard = now + 1
        for core in (0, 1):
            before = clock.monotonic_ns()
            pc = client.read(core, 0x0F)
            after = clock.monotonic_ns()
            if pc in (0, 0xFFFFFFFF):
                raise ProfileError(f"DEBUGPC is invalid on CPU{core}; no intrusive fallback is allowed")
            emit({"core": core, "pc": pc, "host_before_ns": before, "host_after_ns": after})
            count[core] += 1
        next_sample += (1 / hz) * rng.uniform(0.8, 1.2)
        if next_sample < clock.monotonic():
            missed_slots += 1
            next_sample = clock.monotonic() + 1 / hz
    client.running()
    return {"counts": dict(count), "elapsed_seconds": clock.monotonic() - started,
            "missed_host_slots": missed_slots, "unit": "observed_pc_samples"}


def sample_pmu(client, event: str, seconds: float, *, clock=time) -> dict:
    if event not in PMU_EVENTS or not 0 < seconds <= 5:
        raise ProfileError("PMU windows must be at most five seconds")
    client.running()
    saved = {(core, reg): client.read(core, reg) for core in (0, 1) for reg in PMU_REGISTERS}
    if any(saved.values()):
        raise ProfileError("PMU registers already in use or contain another owner's state")
    configured = []
    result = []
    select, mask = PMU_EVENTS[event]
    try:
        starts = {}
        for core in (0, 1):
            configured.append(core)
            client.write(core, 0x28, 0)
            client.write(core, 0x29, 0)
            client.write(core, 0x30, (1 << 16) | (7 << 4))
            client.write(core, 0x31, (mask << 16) | (select << 8) | (7 << 4))
            client.write(core, 0x20, 1)
            starts[core] = clock.monotonic_ns()
        clock.sleep(seconds)
        client.running()
        for core in (0, 1):
            client.write(core, 0x20, 0)
            ended = clock.monotonic_ns()
            statuses = [client.read(core, reg) for reg in (0x38, 0x39)]
            if any(status & 1 for status in statuses):
                raise ProfileError(f"CPU{core} PMU overflow; window rejected")
            result.append({"core": core, "cycles": client.read(core, 0x28),
                           event: client.read(core, 0x29), "host_elapsed_ns": ended - starts[core]})
    finally:
        restore_errors = []
        for core in configured:
            for reg in (0x20, 0x30, 0x31, 0x28, 0x29, 0x20):
                try:
                    client.write(core, reg, saved[core, reg])
                except (OSError, ProfileError) as error:
                    restore_errors.append(f"CPU{core} register {reg:#x}: {error}")
        for core in configured:
            for reg in PMU_REGISTERS:
                try:
                    if client.read(core, reg) != saved[core, reg]:
                        restore_errors.append(f"CPU{core} register {reg:#x}: readback mismatch")
                except (OSError, ProfileError) as error:
                    restore_errors.append(f"CPU{core} register {reg:#x}: readback failed: {error}")
        if restore_errors:
            error = ProfileError("PMU restore was not fully verified: " + "; ".join(restore_errors))
            error.cleanup_errors = restore_errors
            raise error
    client.running()
    return {"event": event, "cores": result, "unit": "whole_core_hardware_events"}


class SystemViewLog(AbstractContextManager):
    """Bind the live endpoint's flushed logger to one bounded acquisition."""

    RECOVERY = re.compile(r"CRC mismatch|CRC[^\n]*(?:fail|recover)|trying with halted target|"
                          r"(?:Failed to write buffs|All write attempts failed)", re.IGNORECASE)
    INTERRUPTION = re.compile(r"(?:\b|_)(?:halt(?:ed|ing)?|resum(?:e|ed|ing)|reset(?:ting)?)(?:\b|_)",
                              re.IGNORECASE)

    def __init__(self, path: Path | None, output: Path):
        if path is None:
            raise ProfileError("SystemView requires --openocd-log for transport qualification")
        self.path, self.output = Path(path), output
        if self.path.resolve() == (output / "sysview-openocd.log").resolve():
            raise ProfileError("SystemView source log must differ from its archived append")
        self.stream = os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb")
        before = os.fstat(self.stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            self.stream.close()
            raise ProfileError("SystemView OpenOCD log must be a regular file")
        self.offset = before.st_size
        try:
            self.prefix_hash = self._prefix_hash()
        except BaseException:
            self.stream.close()
            raise
        self.nonce = uuid.uuid4().hex
        self.markers = {}
        self.metadata = {"status": "invalid", "source": str(self.path),
                         "device": before.st_dev, "inode": before.st_ino,
                         "start_offset": self.offset, "prefix_sha256": self.prefix_hash,
                         "marker_scope": "host logger only; no target instrumentation"}

    def __exit__(self, *args):
        self.stream.close()

    def _prefix_hash(self):
        self.stream.seek(0)
        digest = hashlib.sha256()
        remaining = self.offset
        while remaining:
            block = self.stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise ProfileError("SystemView OpenOCD log was truncated")
            digest.update(block)
            remaining -= len(block)
        return digest.hexdigest()

    def mark(self, client, phase):
        marker = f"ESP_PROFILE_SYSVIEW_{self.nonce}_{phase}"
        client.command(f"echo {{{marker}}}")
        self.markers[phase] = marker
        if phase == "BOUND":
            # LOG_USER writes and fflush() complete before echo returns. An
            # unrelated or redirected log must fail before starting AppTrace.
            self.stream.seek(self.offset)
            if not self._marker_lines(self.stream.read(), marker):
                raise ProfileError("SystemView OpenOCD log is not bound to this endpoint")

    @staticmethod
    def _marker_lines(data, marker):
        positions, offset = [], 0
        for line in data.splitlines(keepends=True):
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            # At verbosity3+, command.c prefixes LOG_USER with its source.
            # Ignore debug command echoes containing the same nonce.
            if text == marker or (text.startswith("User : ") and "handle_echo()" in text
                                  and text.endswith(": " + marker)):
                positions.append((offset, offset + len(line)))
            offset += len(line)
        return positions

    def qualify(self, transport):
        self.stream.seek(self.offset)
        data = self.stream.read()
        archive = self.output / "sysview-openocd.log"
        archive.write_bytes(data)
        self.metadata.update(append_file=archive.name, append_bytes=len(data), append_sha256=sha256(archive))
        current, named = os.fstat(self.stream.fileno()), self.path.stat()
        if ((named.st_dev, named.st_ino) != (self.metadata["device"], self.metadata["inode"]) or
                (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)):
            raise ProfileError("SystemView OpenOCD log was rotated or replaced")
        if current.st_size < self.offset or self._prefix_hash() != self.prefix_hash:
            raise ProfileError("SystemView OpenOCD log was truncated or its prefix changed")
        positions = {phase: self._marker_lines(data, self.markers.get(phase, ""))
                     for phase in ("BOUND", "BEGIN", "END")}
        if any(len(matches) != 1 for matches in positions.values()):
            raise ProfileError("SystemView OpenOCD log markers are missing or ambiguous")
        bound, begin, end = (positions[p][0] for p in ("BOUND", "BEGIN", "END"))
        if not bound[1] <= begin[0] < begin[1] <= end[0]:
            raise ProfileError("SystemView OpenOCD log marker order is invalid")
        self.metadata.update(end_offset=self.offset + len(data),
                             active_byte_range=[self.offset + begin[1], self.offset + end[0]])
        text = data.decode("utf-8", errors="replace")
        replies = "\n".join(transport.get(key, "") for key in ("start_reply", "stop_reply"))
        if self.RECOVERY.search(text + "\n" + replies):
            raise ProfileError("SystemView CRC or recovery activity invalidates timing; evidence preserved")
        active = data[begin[1]:end[0]].decode("utf-8", errors="replace")
        if self.INTERRUPTION.search(active):
            raise ProfileError("SystemView halt, resume or reset during the active interval invalidates timing")
        self.metadata["status"] = "qualified"


def record_sysview(client, output: Path, seconds: float, idf_python: Path, idf_root: Path,
                   elf: Path, toolchain: str, *, openocd_log: Path | None = None, clock=time) -> dict:
    """Official RTOS hooks, admitted only with uninterrupted transport evidence.

    Native attach/detach may halt transiently, outside host logger markers.
    The analyzer additionally excludes the first and last second.
    """
    if not 3 <= seconds <= 30:
        raise ProfileError("SystemView captures must be between three and thirty seconds")
    files = [output.resolve() / f"core{core}.SVDat" for core in (0, 1)]
    if any(re.search(r"[{}\\\x00-\x1f]", str(path)) for path in files):
        raise ProfileError("Unsupported characters in SystemView output path")
    parser = idf_root / "tools/esp_app_trace/sysviewtrace_proc.py"
    if not parser.is_file() or not idf_python.is_file():
        raise ProfileError("Official ESP-IDF SystemView parser and its Python environment are required")
    with SystemViewLog(openocd_log, output) as evidence:
        client.running()
        status = client.command("esp sysview status")
        if not re.search(r"Tracing is (?:STOPPED|not running)", status):
            raise ProfileError("SystemView is already owned or its status cannot be verified")
        level = client.command("debug_level")
        if not level.isdecimal() or int(level) < 2:
            raise ProfileError("SystemView requires OpenOCD logging at INFO level or above")
        evidence.mark(client, "BOUND")
        started, active, primary = False, False, None
        transport = {"status_before": status, "openocd_log": evidence.metadata}
        try:
            reply = client.command(f"esp sysview start {{file://{files[0]}}} {{file://{files[1]}}} 1 33554432 -1")
            transport["start_reply"] = reply
            started = True  # A completed START reply may report an already active failure.
            if "error" in reply.lower() or "failed" in reply.lower():
                raise ProfileError(f"SystemView start failed: {reply}")
            evidence.mark(client, "BEGIN")
            active = True
            clock.sleep(seconds)
        except BaseException as error:
            primary = error
            raise
        finally:
            errors = []
            if started:
                if active and not getattr(client, "reply_in_flight", False):
                    try:
                        evidence.mark(client, "END")
                    except (OSError, ProfileError) as error:
                        errors.append(error)
                try:
                    if getattr(client, "reply_in_flight", False):
                        raise ProfileError("Incomplete Tcl reply; SystemView stop is unverified")
                    transport["stop_reply"] = client.command("esp sysview stop")
                    transport["status_after"] = client.command("esp sysview status")
                except (OSError, ProfileError) as error:
                    transport["stop_error"] = repr(error)
                    errors.append(error)
            try:
                evidence.qualify(transport)
            except (OSError, ProfileError) as error:
                evidence.metadata["error"] = str(error)
                errors.append(error)
            write_json(output / "sysview-transport.json", transport)
            if errors:
                if primary is None:
                    raise errors[0]
                primary.cleanup_errors = [*getattr(primary, "cleanup_errors", []), *(str(e) for e in errors)]
    statistics = re.findall(r"Data: blocks incomplete (\d+), lost bytes: (\d+)", transport.get("stop_reply", ""))
    if (not statistics or any(int(incomplete) or int(lost) for incomplete, lost in statistics) or
            "Tracing is STOPPED" not in transport.get("status_after", "")):
        raise ProfileError("SystemView transport loss or unverified final stop; raw evidence preserved")
    client.running()
    if not all(path.is_file() and path.stat().st_size for path in files):
        raise ProfileError("Missing or empty per-core SystemView trace")
    with (output / "sysview.json").open("w") as stdout, (output / "sysview-parser.log").open("w") as stderr:
        result = subprocess.run([str(idf_python), str(parser), "--to-json", "--elf-file", str(elf),
                                 "--toolchain", toolchain, *(str(path) for path in files)],
                                stdout=stdout, stderr=stderr, timeout=60, check=False)
    metadata = {"unit": "official_rtos_events", "parser_exit_code": result.returncode,
                "method": "official_freertos_hooks_with_qualified_apptrace_transport",
                "openocd_log": evidence.metadata,
                "trim_seconds": 1, "files": {path.name: sha256(path) for path in files}}
    write_json(output / "sysview-acquisition.json", metadata)
    if result.returncode:
        raise ProfileError("SystemView parser failed; raw traces and partial JSON were preserved")
    return metadata


def symbolize(elf: Path, addresses: list[int], addr2line: str) -> dict:
    if not addresses:
        return {}
    result = subprocess.run([addr2line, "-a", "-f", "-C", "-i", "-e", str(elf)],
                            input="".join(f"0x{pc:x}\n" for pc in addresses),
                            text=True, capture_output=True, check=True, timeout=60)
    symbols = {}
    current = None
    for line in result.stdout.splitlines():
        if re.fullmatch(r"0x[0-9a-fA-F]+", line):
            current = int(line, 16)
            symbols[current] = []
        elif current is not None:
            symbols[current].append(line)
    if set(symbols) != set(addresses):
        raise ProfileError("Incomplete addr2line result")
    return {pc: {"function": lines[0] if lines else "??", "locations": lines[1:]}
            for pc, lines in symbols.items()}


def analyze_pc(rows: list[dict], symbols: dict) -> dict:
    totals = Counter()
    functions = defaultdict(Counter)
    unresolved = Counter()
    for row in rows:
        core, pc = row["core"], row["pc"]
        if core not in (0, 1) or pc == 0 or row["host_after_ns"] < row["host_before_ns"]:
            raise ProfileError("Invalid PC sample")
        name = symbols.get(pc, {}).get("function", "??")
        totals[core] += 1
        if name == "??":
            unresolved[core] += 1
        functions[core][name] += 1
    if set(totals) != {0, 1}:
        raise ProfileError("Both cores must be represented")
    return {"unit": "share_of_valid_pc_samples", "cores": {
        str(core): {"samples": totals[core], "unresolved_samples": unresolved[core],
                    "functions": {name: count / totals[core] for name, count in functions[core].items()}}
        for core in (0, 1)}}


def analyze_sysview(data: dict, parser_exit_code: int, *, trim_seconds=0) -> dict:
    """Use official task events; never turn missing events into zero latency."""
    if parser_exit_code != 0 or data.get("version") != "1.0":
        raise ProfileError("SystemView parser failed or its schema is unsupported")
    events = data.get("events", [])
    if any(not isinstance(e.get("ts"), (int, float)) or not math.isfinite(e["ts"]) or e["ts"] < 0
           for e in events):
        raise ProfileError("Invalid SystemView timestamp")
    cores = {e.get("core_id") for e in events}
    if not {0, 1} <= cores:
        raise ProfileError("SystemView capture must include both cores")
    if any(e.get("id") == 1 for e in events):
        raise ProfileError("SystemView overflow makes timing attribution incomplete")
    spans = {core: [e["ts"] for e in events if e["core_id"] == core] for core in (0, 1)}
    beginning = min(min(values) for values in spans.values())
    ending = max(max(values) for values in spans.values())
    if trim_seconds:
        beginning = max(min(values) for values in spans.values()) + trim_seconds
        ending = min(max(values) for values in spans.values()) - trim_seconds
        if ending <= beginning:
            raise ProfileError("No common steady-state SystemView interval remains")
    ready, running, residency, waits = {}, {}, Counter(), defaultdict(list)
    by_core = defaultdict(Counter)
    blocked, task_info = set(), {}
    censored = 0
    starts, ends = {}, {}

    def overlap(start, stop):
        return max(0, min(stop, ending) - max(start, beginning))

    def credit(core, tid, start, stop):
        duration = overlap(start, stop)
        residency[str(tid)] += duration
        by_core[core][str(tid)] += duration

    for event in sorted(events, key=lambda e: e["ts"]):
        stamp, core, eid = event["ts"], event["core_id"], event["id"]
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            raise ProfileError("Invalid SystemView timestamp")
        starts.setdefault(core, stamp)
        ends[core] = stamp
        tid = event.get("params", {}).get("tid")
        if eid == 9 and tid is not None:
            task_info[str(tid)] = event.get("params", {})
        elif eid == 6 and tid is not None:
            blocked.discard(tid)
            if not any(active_tid == tid for active_tid, _ in running.values()):
                ready.setdefault(tid, stamp)
        elif eid in (4, 5, 11, 17):
            previous = running.pop(core, None)
            if previous is not None:
                old_tid, since = previous
                credit(core, old_tid, since, stamp)
                if eid in (4, 5) and isinstance(old_tid, int) and old_tid not in blocked and old_tid != tid:
                    ready.setdefault(old_tid, stamp)
            if eid == 4 and tid is not None:
                if any(other_tid == tid for other_tid, _ in running.values()):
                    raise ProfileError("A task cannot execute on both cores simultaneously")
                running[core] = (tid, stamp)
                blocked.discard(tid)
                if tid in ready:
                    since = ready.pop(tid)
                    if beginning <= since <= stamp <= ending:
                        waits[str(tid)].append(stamp - since)
                    elif overlap(since, stamp):
                        censored += 1
            elif eid == 17:
                running[core] = (f"IDLE{core}", stamp)
        elif eid == 7 and tid is not None:
            # STOP_READY denotes blocking/suspension, not dispatch. An
            # interrupted ready interval cannot be credited as a zero wait.
            censored += int(tid in ready and overlap(ready[tid], stamp) > 0)
            ready.pop(tid, None)
            blocked.add(tid)
            for active_core, (active_tid, since) in list(running.items()):
                if active_tid == tid:
                    credit(active_core, tid, since, stamp)
                    del running[active_core]
        elif eid in (8, 29, 33):
            # 29 is SEGGER task termination; 33 is the official FreeRTOS
            # vTaskDelete event. ID reuse starts a fresh lifetime.
            deleted_tid = tid if tid is not None else event.get("params", {}).get("xTaskToDelete")
            if deleted_tid is not None:
                censored += int(deleted_tid in ready and overlap(ready[deleted_tid], stamp) > 0)
                ready.pop(deleted_tid, None)
                blocked.discard(deleted_tid)
                for active_core, (active_tid, since) in list(running.items()):
                    if active_tid == deleted_tid:
                        credit(active_core, deleted_tid, since, stamp)
                        del running[active_core]
    for core, (tid, since) in running.items():
        credit(core, tid, since, ends[core])
    core_spans = {str(c): overlap(starts[c], ends[c]) for c in (0, 1)}
    return {"unit": "seconds", "cpu_attribution": "scheduled_residency_including_untraced_interrupts",
            "scheduled_residency": dict(residency), "ready_to_run": {tid: distribution(v) for tid, v in waits.items()},
            "task_metadata_last_observed": task_info,
            "scheduled_by_core": {str(c): dict(by_core[c]) for c in (0, 1)},
            "unattributed_seconds": {str(c): max(0, core_spans[str(c)] - sum(by_core[c].values())) for c in (0, 1)},
            "censored_ready_intervals": sum(since <= ending for since in ready.values()) + censored,
            "censored_running_intervals": sum(since < ending and ends[c] <= ending
                                                for c, (_, since) in running.items()),
            "core_span_seconds": core_spans}


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"count": len(values), "mean": statistics.mean(values), "max": max(values),
            **{f"p{p}": ordered[max(0, math.ceil(len(values) * p / 100) - 1)] for p in (50, 95, 99)}}


def compare(baselines: list[dict], candidates: list[dict]) -> dict:
    if not baselines or not candidates:
        raise ProfileError("Both baseline and candidate captures are required")
    runs = baselines + candidates
    contracts = [r["contract"] for r in runs]
    if not runs or any(c != contracts[0] for c in contracts):
        raise ProfileError("Comparison requires identical scenario, profile and acquisition contracts")
    if any(r.get("status") != "valid" for r in runs):
        raise ProfileError("Invalid captures cannot be compared")
    run_ids = [r.get("capture_id") for r in runs]
    if not all(run_ids) or len(set(run_ids)) != len(run_ids):
        raise ProfileError("Independent capture identities are required; repeated files are not new windows")
    if any(r["analysis"].get("unit") != "share_of_valid_pc_samples" for r in runs):
        raise ProfileError("This comparison supports PC sample shares only")
    changes = {}
    for core in ("0", "1"):
        names = set().union(*(r["analysis"]["cores"][core]["functions"] for r in runs))
        changes[core] = {}
        for name in names:
            before = [r["analysis"]["cores"][core]["functions"].get(name, 0) for r in baselines]
            after = [r["analysis"]["cores"][core]["functions"].get(name, 0) for r in candidates]
            entry = {"baseline_mean": statistics.mean(before), "candidate_mean": statistics.mean(after),
                     "delta": statistics.mean(after) - statistics.mean(before)}
            if min(len(before), len(after)) >= 5:
                rng = random.Random(0)
                bootstrap = sorted(statistics.mean(rng.choices(after, k=len(after))) -
                                   statistics.mean(rng.choices(before, k=len(before))) for _ in range(1000))
                entry["run_bootstrap_95_interval"] = [bootstrap[24], bootstrap[974]]
            changes[core][name] = entry
    return {"unit": "share_of_valid_pc_samples", "baseline_runs": len(baselines),
            "candidate_runs": len(candidates), "repeated_windows": min(len(baselines), len(candidates)) >= 5,
            "changes": changes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check local artifacts without connecting to a device")
    doctor.add_argument("--elf", required=True, type=Path)
    doctor.add_argument("--manifest", required=True, type=Path)
    doctor.add_argument("--openocd", required=True, type=Path)
    record = commands.add_parser("record", help="Acquire qualified DEBUGPC or PMU data from local OpenOCD")
    record.add_argument("--elf", required=True, type=Path)
    record.add_argument("--manifest", required=True, type=Path)
    record.add_argument("--output", required=True, type=Path)
    record.add_argument("--port", type=int, default=6666)
    record.add_argument("--mode", choices=("pc", "pmu", "sysview"), default="pc")
    record.add_argument("--seconds", type=float, default=60)
    record.add_argument("--hz", type=float, default=100)
    record.add_argument("--event", choices=tuple(PMU_EVENTS), default="instructions")
    record.add_argument("--scenario", required=True)
    record.add_argument("--idf-python", type=Path)
    record.add_argument("--idf-root", type=Path)
    record.add_argument("--openocd-log", type=Path,
                        help="Required for SystemView: live endpoint log at INFO level or above")
    record.add_argument("--toolchain", default="xtensa-esp32s3-elf-")
    analyze = commands.add_parser("analyze")
    analyze.add_argument("capture", type=Path)
    analyze.add_argument("--elf", type=Path)
    analyze.add_argument("--addr2line", default="xtensa-esp32s3-elf-addr2line")
    analyze.add_argument("--sysview-json", type=Path)
    analyze.add_argument("--parser-exit-code", type=int)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--baseline", nargs="+", type=Path, required=True)
    comparison.add_argument("--candidate", nargs="+", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            manifest = identity(args.elf, args.manifest)
            version = subprocess.run([str(args.openocd), "--version"], capture_output=True, text=True, check=True)
            output = version.stdout + version.stderr
            if OPENOCD_VERSION not in output:
                raise ProfileError("OpenOCD version is not qualified for this register contract")
            print(json.dumps({"elf_sha256": manifest["elf_sha256"], "openocd_sha256": sha256(args.openocd),
                              "openocd_source_revision": OPENOCD_REVISION, "device_contacted": False}))
        elif args.command == "record":
            manifest = identity(args.elf, args.manifest)
            if args.mode == "pmu" and manifest.get("apptrace_enabled") is not False:
                raise ProfileError("PMU acquisition requires confirmed AppTrace disabled")
            if args.mode == "sysview" and not (manifest.get("apptrace_enabled") is True and
                                               manifest.get("systemview_enabled") is True and
                                               args.idf_python and args.idf_root and args.openocd_log):
                raise ProfileError("SystemView requires an identified diagnostic build, official IDF parser and --openocd-log")
            boot_validation = validate_sysview_boot(manifest) if args.mode == "sysview" else None
            args.output.mkdir(parents=True, exist_ok=False)
            report = {"version": 1, "capture_id": uuid.uuid4().hex, "status": "invalid", "mode": args.mode, "identity": manifest,
                      "clock_anchor": {"monotonic_before_ns": time.monotonic_ns(),
                                       "realtime_ns": time.time_ns(),
                                       "monotonic_after_ns": time.monotonic_ns()},
                      "contract": {"scenario": args.scenario, "profile": manifest["profile"],
                                   "scenario_contract": manifest["scenario_contract"], "mode": args.mode,
                                   "hz": args.hz if args.mode == "pc" else None, "seconds": args.seconds,
                                   "pmu_event": args.event if args.mode == "pmu" else None}}
            if boot_validation is not None:
                report["boot_validation"] = boot_validation
            try:
                with OpenOCD("127.0.0.1", args.port, systemview=args.mode == "sysview") as client:
                    report["openocd_version"] = client.command("version")
                    if OPENOCD_VERSION not in report["openocd_version"]:
                        raise ProfileError("Unqualified OpenOCD endpoint")
                    if args.mode == "pc":
                        with (args.output / "pc.jsonl").open("w") as stream:
                            report["acquisition"] = sample_pcs(client, args.seconds, args.hz,
                                lambda row: stream.write(json.dumps(row) + "\n"))
                    elif args.mode == "pmu":
                        report["acquisition"] = sample_pmu(client, args.event, args.seconds)
                    else:
                        report["acquisition"] = record_sysview(client, args.output, args.seconds,
                            args.idf_python, args.idf_root, args.elf, args.toolchain, openocd_log=args.openocd_log)
                report["status"] = "valid"
            except (OSError, ProfileError, KeyboardInterrupt) as error:
                report["error"] = str(error) or type(error).__name__
                if hasattr(error, "cleanup_errors"):
                    report["pmu_cleanup_errors" if args.mode == "pmu" else "transport_cleanup_errors"] = error.cleanup_errors
                raise
            finally:
                write_json(args.output / "capture.json", report)
        elif args.command == "analyze":
            report = json.loads((args.capture / "capture.json").read_text())
            if report.get("status") != "valid":
                raise ProfileError("Capture was not completed successfully")
            if report["mode"] == "sysview":
                log = report["acquisition"].get("openocd_log", {})
                if (log.get("status") != "qualified" or log.get("append_file") != "sysview-openocd.log" or
                        not (args.capture / "sysview-openocd.log").is_file() or
                        sha256(args.capture / "sysview-openocd.log") != log.get("append_sha256")):
                    raise ProfileError("SystemView lacks hash-bound uninterrupted OpenOCD log evidence")
                report["analysis"] = analyze_sysview(json.loads((args.capture / "sysview.json").read_text()),
                    report["acquisition"]["parser_exit_code"], trim_seconds=report["acquisition"]["trim_seconds"])
            elif args.sysview_json:
                raise ProfileError("SystemView JSON requires its own qualified SystemView capture and OpenOCD log")
            elif report["mode"] == "pc":
                if args.elf is None or sha256(args.elf) != report["identity"]["elf_sha256"]:
                    raise ProfileError("Analysis requires the exact captured ELF")
                rows = [json.loads(line) for line in (args.capture / "pc.jsonl").read_text().splitlines()]
                symbols = symbolize(args.elf, sorted({r["pc"] for r in rows}), args.addr2line)
                report["analysis"] = analyze_pc(rows, symbols)
            else:
                report["analysis"] = report["acquisition"]
            write_json(args.capture / "analysis.json", report)
        else:
            write_json(args.output, compare([json.loads(p.read_text()) for p in args.baseline],
                                           [json.loads(p.read_text()) for p in args.candidate]))
    except (ProfileError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f"esp_profile: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
