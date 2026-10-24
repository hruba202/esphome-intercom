"""Profiling transport, provenance and scheduling oracles, without ESP hardware."""

import json
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from tools import esp_profile
from tools.esp_profile import (
    OpenOCD,
    ProfileError,
    analyze_pc,
    analyze_sysview,
    compare,
    identity,
    record_sysview,
    sample_pcs,
    sample_pmu,
    sha256,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def monotonic_ns(self):
        return int(self.now * 1_000_000_000)

    def sleep(self, seconds):
        self.now += seconds


def sysview_logger_reply(command, logfile):
    if command == 'debug_level':
        return '2'
    if command.startswith('echo {'):
        with logfile.open('a') as stream:
            stream.write(command[6:-1] + '\n')
        return ''
    return None


def test_systemview_stop_has_a_budget_for_native_drain(monkeypatch):
    host, server = socket.socketpair()
    received = []

    def peer():
        with server:
            request = b''
            while not request.endswith(b'\x1a'):
                request += server.recv(1024)
            received.append(request.decode())
            # Native STOP has its own five-second drain deadline, plus JTAG
            # reads. Scale both budgets down to test the real socket behavior.
            time.sleep(0.08)
            try:
                server.sendall(b'Tracing is STOPPED\x1a')
            except BrokenPipeError:
                pass

    worker = threading.Thread(target=peer)
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    try:
        with OpenOCD('127.0.0.1', 6666, timeout=0.02, systemview=True) as client:
            assert client.command('esp sysview stop') == 'Tracing is STOPPED'
            assert client.socket.gettimeout() == pytest.approx(0.02)
    finally:
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert received == ['capture {esp sysview stop}\x1a']


def test_incomplete_tcl_reply_does_not_accept_cleanup_as_a_new_reply(monkeypatch):
    host, server = socket.socketpair()
    received = []

    def peer():
        with server:
            request = b''
            while not request.endswith(b'\x1a'):
                request += server.recv(1024)
            received.append(request.decode())
            server.sendall(b'partial reply')
            while data := server.recv(1024):
                received.append(data.decode())
                server.sendall(b'\x1a')

    worker = threading.Thread(target=peer)
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    with pytest.raises(TimeoutError) as failure:
        with OpenOCD('127.0.0.1', 6666, timeout=0.02) as client:
            client.poll_owned = True
            client.command('version')
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert received == ['version\x1a'], 'An unfinished reply must not consume a cleanup command'
    assert failure.value.cleanup_errors, 'Unverified polling restoration must be reported'


class Device:
    def __init__(self):
        self.registers = {(core, reg): 0 for core in (0, 1) for reg in (0x20, 0x28, 0x29, 0x30, 0x31)}
        self.writes = []
        self.guards = 0
        self.zero_pc = False
        self.overflow = False

    def running(self):
        self.guards += 1

    def read(self, core, register):
        if register == 0x0F:
            return 0 if self.zero_pc else 0x42001000 + core * 16
        if register in (0x38, 0x39):
            return int(self.overflow)
        return self.registers[core, register]

    def write(self, core, register, value):
        self.writes.append((core, register, value))
        self.registers[core, register] = value


def serve_nar(server, received, replies, *, polling='on'):
    with server:
        while True:
            request = b''
            while not request.endswith(b'\x1a'):
                data = server.recv(1024)
                if not data:
                    return
                request += data
            text = request.decode()
            received.append(text)
            if text == 'capture {poll}\x1a':
                reply = f'background polling: {polling}\nTAP: esp32s3.tap0 (enabled)'.encode()
            elif text in ('capture {poll off}\x1a', 'capture {poll on}\x1a'):
                polling = 'off' if ' off}' in text else 'on'
                reply = b''
            else:
                assert polling == 'off', 'Raw scans must own the debugger polling state'
                reply = replies.pop(0)
            server.sendall(reply[:4])
            server.sendall(reply[4:] + b'\x1a')


def test_pc_collection_reads_both_cores_with_jitter_and_no_fallback():
    device, clock, rows = Device(), Clock(), []
    result = sample_pcs(device, 0.1, 100, rows.append, clock=clock)
    assert 8 <= result['counts'][0] <= 12
    assert result['counts'][0] == result['counts'][1]
    assert {r['core'] for r in rows} == {0, 1}
    assert device.writes == []
    assert device.guards >= 2
    device.zero_pc = True
    with pytest.raises(ProfileError, match='no intrusive fallback'):
        sample_pcs(device, 0.1, 100, rows.append, clock=clock)
    assert device.writes == []


def test_tcl_fragmented_replies_and_command_firewall(monkeypatch):
    host, server = socket.socketpair()
    received = []

    worker = threading.Thread(target=serve_nar, args=(server, received, [b'42000001']))
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    with OpenOCD('127.0.0.1', 6666) as client:
        for command in ('halt', 'profile 1 out', 'resume', 'reset run', 'shutdown',
                        'esp32s3.cpu0 xtensa dm 0xf; halt', 'esp32s3.cpu0 xtensa dm 0x43 0x1',
                        'esp32s3.cpu0 xtensa dm 0xf 0x1', 'esp32s3.cpu0 xtensa dm 0xf',
                        'irscan esp32s3.tap0 0x1c'):
            with pytest.raises(ProfileError):
                client.command(command)
        for core, register, value in ((0, 0x0F, 1), (0, 0x43, 1), (0, 0x20, -1), (2, 0x20, 1)):
            with pytest.raises(ProfileError):
                client.write(core, register, value)
        assert client.read(0, 0x0F) == 0x42000001
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert received[:3] == ['capture {poll}\x1a', 'capture {poll off}\x1a', 'capture {poll}\x1a']
    assert received[3] == ('irscan esp32s3.tap0 0x1c -endstate IDLE; '
                           'drscan esp32s3.tap0 8 0x1e -endstate IDLE; '
                           'drscan esp32s3.tap0 32 0x00000000 -endstate IDLE\x1a')
    assert received[4:] == ['capture {poll on}\x1a', 'capture {poll}\x1a']
    with pytest.raises(ProfileError, match='loopback'):
        OpenOCD('192.0.2.1', 6666)


def test_nar_pmu_write_and_read_use_distinct_encoded_addresses(monkeypatch):
    host, server = socket.socketpair()
    requests = []

    worker = threading.Thread(target=serve_nar, args=(server, requests, [b'00000000', b'00000001']))
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    with OpenOCD('127.0.0.1', 6666) as client:
        client.write(1, 0x20, 1)
        assert client.read(1, 0x20) == 1
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert 'drscan esp32s3.tap1 8 0x41 -endstate IDLE' in requests[3]
    assert 'drscan esp32s3.tap1 32 0x00000001 -endstate IDLE' in requests[3]
    assert 'drscan esp32s3.tap1 8 0x40 -endstate IDLE' in requests[4]


def test_failed_nar_read_restores_host_polling_without_halting(monkeypatch):
    host, server = socket.socketpair()
    requests = []
    worker = threading.Thread(target=serve_nar, args=(server, requests, [b'JTAG transport error']))
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    with pytest.raises(ProfileError, match='Invalid JTAG'):
        with OpenOCD('127.0.0.1', 6666) as client:
            client.read(0, 0x0F)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert requests[-2:] == ['capture {poll on}\x1a', 'capture {poll}\x1a']


def test_foreign_polling_owner_is_not_overwritten(monkeypatch):
    host, server = socket.socketpair()
    requests = []
    worker = threading.Thread(target=serve_nar, args=(server, requests, []), kwargs={'polling': 'off'})
    worker.start()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: host)
    with pytest.raises(ProfileError, match='another owner'):
        with OpenOCD('127.0.0.1', 6666) as client:
            client.read(0, 0x0F)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert requests == ['capture {poll}\x1a']


def test_pmu_rejects_foreign_ownership_before_writing_and_restores_on_overflow():
    device, clock = Device(), Clock()
    device.registers[0, 0x31] = 123
    with pytest.raises(ProfileError, match="another owner's"):
        sample_pmu(device, 'instructions', 5, clock=clock)
    assert not device.writes
    device.registers[0, 0x31] = 0
    device.overflow = True
    with pytest.raises(ProfileError, match='overflow'):
        sample_pmu(device, 'instructions', 5, clock=clock)
    assert all(value == 0 for value in device.registers.values())
    assert device.writes


@pytest.mark.parametrize('event', ['instructions', 'data_stalls', 'instruction_stalls', 'waiti'])
def test_pmu_windows_have_per_core_durations_and_leave_hardware_unowned(event):
    device, clock = Device(), Clock()
    result = sample_pmu(device, event, 5, clock=clock)
    assert result['unit'] == 'whole_core_hardware_events'
    assert len(result['cores']) == 2
    assert all(row['host_elapsed_ns'] == 5_000_000_000 for row in result['cores'])
    assert all(value == 0 for value in device.registers.values())
    with pytest.raises(ProfileError, match='five seconds'):
        sample_pmu(device, event, 20, clock=clock)


def test_pmu_cleanup_attempts_both_cores_after_a_transient_restore_failure():
    class RestoreFailure(Device):
        def __init__(self):
            super().__init__()
            self.disables = 0

        def write(self, core, register, value):
            if core == 0 and register == 0x20 and value == 0:
                self.disables += 1
                if self.disables == 2:
                    raise ProfileError('one-shot restore write failure')
            return super().write(core, register, value)

    device = RestoreFailure()
    device.overflow = True
    with pytest.raises(ProfileError, match='restore was not fully verified') as failure:
        sample_pmu(device, 'instructions', 5, clock=Clock())
    assert failure.value.cleanup_errors
    assert all(value == 0 for value in device.registers.values())
    assert (1, 0x20, 0) in device.writes


def test_elf_and_running_image_provenance_are_bound_to_saved_evidence(tmp_path):
    elf = tmp_path / 's3.elf'
    header = bytearray(52)
    header[:6] = b'\x7fELF\x01\x01'
    header[18:20] = (94).to_bytes(2, 'little')
    elf.write_bytes(header)
    evidence = tmp_path / 'verified-upload.json'
    evidence.write_text('{"method":"test-only-upload-oracle"}')
    manifest = {
        'elf_sha256': sha256(elf), 'device_mac': '00:00:00:00:00:00', 'profile': 'fixture',
        'scenario_contract': {'rate': 48000},
        'source_revisions': {'audio': {'head': 'a' * 40, 'tree_sha256': 'b' * 64, 'dirty': False}},
        'firmware_sha256': 'c' * 64, 'running_identity': {
            'elf_sha256': sha256(elf), 'evidence': str(evidence), 'evidence_sha256': sha256(evidence),
        },
    }
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    assert identity(elf, path)['profile'] == 'fixture'
    evidence.write_text('different device')
    with pytest.raises(ProfileError, match='evidence'):
        identity(elf, path)
    elf.write_bytes(b'\x7fELF' + b'\x00' * 48)
    with pytest.raises(ProfileError, match='Xtensa'):
        identity(elf, path)


@pytest.fixture
def boot_evidence(tmp_path):
    """The saved upload/API/runtime receipts used by the real host workflow."""
    elf = tmp_path / 's3.elf'
    header = bytearray(52)
    header[:6] = b'\x7fELF\x01\x01'
    header[18:20] = (94).to_bytes(2, 'little')
    elf.write_bytes(header)
    device = {'mac_address': '00:11:22:33:44:55', 'compilation_time': '2026-10-23 19:33:22 +0000',
              'esphome_version': '2026.10.0'}
    manifest = {'elf_sha256': sha256(elf), 'firmware_sha256': 'c' * 64,
                'device_mac': device['mac_address'], 'profile': 'fixture', 'scenario_contract': {'rate': 48000},
                'source_revisions': {'audio': {'head': 'a' * 40, 'tree_sha256': 'b' * 64, 'dirty': False}},
                'apptrace_enabled': True, 'systemview_enabled': True}
    before_marker = {'identity': dict(device), 'verified_at': 1007.0, 'audio': {'uptime_ms': '7000'}}
    upload = {'status': 'uploaded_and_verified', 'target': {'mac': device['mac_address']},
              'elf_sha256': manifest['elf_sha256'], 'binary_sha256': manifest['firmware_sha256'],
              'build': {'build_time_str': device['compilation_time'], 'esphome_version': device['esphome_version']},
              'after': before_marker, 'finished_at': 1061.0,
              'boot_confirmation': {'observed_log': [{'at': 1060.0, 'message':
                  '[I][safe_mode:154]: Boot seems successful; resetting boot loop counter'}],
                  'identity_after_boot_success': dict(device)}}
    post = {'identity': dict(device), 'verified_at': 1080.0, 'audio': {'uptime_ms': '80000'},
            # This is a cached sensor state on reconnect, not an instant measurement.
            'heap_observations': {'Uptime': {'value': 2.0, 'api_received_at': 1080.0}}}
    runtime = {'elf_sha256': manifest['elf_sha256'], 'live_app_elf_sha256': manifest['elf_sha256'],
               'verified_at': 1075.0}
    files = {'boot_confirmation': (tmp_path / 'upload.json', upload),
             'post_prefit_evidence': (tmp_path / 'post-prefit.json', post),
             'runtime_preflight_evidence': (tmp_path / 'runtime-map.json', runtime)}
    def save():
        for key, (path, data) in files.items():
            path.write_text(json.dumps(data))
            manifest[key] = {'evidence': str(path), 'evidence_sha256': sha256(path)}
        manifest['running_identity'] = {**manifest['boot_confirmation'], 'elf_sha256': manifest['elf_sha256']}
        path = tmp_path / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return path
    return SimpleNamespace(elf=elf, manifest=manifest, upload=upload, post=post, runtime=runtime,
                           device=device, files=files, save=save, root=tmp_path)


@pytest.mark.parametrize('failure', ['missing_boot', 'missing_post', 'missing_runtime', 'marker', 'upload_mac',
    'upload_elf', 'upload_binary', 'after_marker_mac', 'post_build', 'before_marker', 'live_elf', 'checksum',
    'cached_uptime_only', 'reboot_stale_map'])
def test_systemview_rejects_unconfirmed_or_stale_boot_before_contact(boot_evidence, monkeypatch, capsys, failure):
    proof = boot_evidence
    if failure == 'marker':
        proof.upload['boot_confirmation']['observed_log'][0]['message'] = 'API connected'
    elif failure == 'upload_mac':
        proof.upload['target']['mac'] = '00:11:22:33:44:66'
    elif failure == 'upload_elf':
        proof.upload['elf_sha256'] = 'd' * 64
    elif failure == 'upload_binary':
        proof.upload['binary_sha256'] = 'd' * 64
    elif failure == 'after_marker_mac':
        proof.upload['boot_confirmation']['identity_after_boot_success']['mac_address'] = '00:11:22:33:44:66'
    elif failure == 'post_build':
        proof.post['identity']['compilation_time'] = 'previous build'
    elif failure == 'before_marker':
        proof.post['verified_at'] = 1050.0
    elif failure == 'live_elf':
        proof.runtime['live_app_elf_sha256'] = 'd' * 64
    elif failure == 'cached_uptime_only':
        proof.post.pop('audio')
    elif failure == 'reboot_stale_map':
        proof.post['audio']['uptime_ms'] = '2000'
    manifest = proof.save()
    missing = {'missing_boot': 'boot_confirmation', 'missing_post': 'post_prefit_evidence',
               'missing_runtime': 'runtime_preflight_evidence'}
    if failure in missing:
        proof.manifest.pop(missing[failure])
        manifest.write_text(json.dumps(proof.manifest))
    elif failure == 'checksum':
        proof.files['post_prefit_evidence'][0].write_text('{}')
    contacted = []
    def forbidden_contact(*args, **kwargs):
        contacted.append(True)
        raise ProfileError('Debugger contacted before boot evidence was admitted')
    monkeypatch.setattr(esp_profile, 'OpenOCD', forbidden_contact)
    monkeypatch.setattr('sys.argv', ['esp_profile', 'record', '--mode', 'sysview', '--elf', str(proof.elf),
        '--manifest', str(manifest), '--scenario', 'idle', '--seconds', '5', '--output', str(proof.root / 'capture'),
        '--idf-root', str(proof.root), '--idf-python', 'python', '--openocd-log', str(proof.root / 'openocd.log')])
    with pytest.raises(SystemExit) as result:
        esp_profile.main()
    assert result.value.code == 2
    assert not contacted, 'Invalid boot/preflight evidence must be rejected before contacting the debugger'
    assert 'SystemView' in capsys.readouterr().err


@pytest.mark.parametrize('rebooted', [False, True])
def test_systemview_admits_confirmed_boot_or_fresh_post_reboot_runtime_map(boot_evidence, monkeypatch, rebooted):
    proof = boot_evidence
    if rebooted:
        proof.post['audio']['uptime_ms'] = '10000'
        proof.runtime['verified_at'] = 1075.0
    manifest = proof.save()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def command(self, command):
            assert command == 'version'
            return esp_profile.OPENOCD_VERSION

    monkeypatch.setattr(esp_profile, 'OpenOCD', Client)
    monkeypatch.setattr(esp_profile, 'record_sysview', lambda *a, **kw: {'method': 'test-only-trace'})
    monkeypatch.setattr('sys.argv', ['esp_profile', 'record', '--mode', 'sysview', '--elf', str(proof.elf),
        '--manifest', str(manifest), '--scenario', 'idle', '--seconds', '5', '--output', str(proof.root / 'capture'),
        '--idf-root', str(proof.root), '--idf-python', 'python', '--openocd-log', str(proof.root / 'openocd.log')])
    assert esp_profile.main() == 0
    report = json.loads((proof.root / 'capture/capture.json').read_text())
    assert report['boot_validation']['reboot_after_confirmation'] is rebooted
    assert report['boot_validation']['uptime_source'] == 'requested_audio_diagnostic'
    assert report['boot_validation']['partition_state_read_directly'] is False


def event(eid, stamp, core=0, tid=None, **params):
    if tid is not None:
        params['tid'] = tid
    return {'id': eid, 'ts': stamp, 'core_id': core, 'params': params}


def test_systemview_ready_latency_is_not_inferred_from_pc_or_parser_failure():
    data = {'version': '1.0', 'events': [
        event(10, 0), event(10, 0, 1), event(6, .01, tid=7), event(4, .04, tid=7),
        event(5, .09), event(6, .10, tid=7), event(7, .11, tid=7),
        event(4, .12, tid=7), event(17, .16), event(11, .2), event(11, .2, 1),
    ]}
    result = analyze_sysview(data, 0)
    assert result['unit'] == 'seconds'
    assert result['ready_to_run']['7']['count'] == 1
    assert result['ready_to_run']['7']['p99'] == pytest.approx(.03)
    assert result['scheduled_residency']['7'] == pytest.approx(.09)
    assert result['censored_ready_intervals'] == 1
    assert 'untraced_interrupts' in result['cpu_attribution']
    with pytest.raises(ProfileError, match='parser failed'):
        analyze_sysview(data, 2)
    with pytest.raises(ProfileError, match='overflow'):
        analyze_sysview({'version': '1.0', 'events': data['events'] + [event(1, .15, drop_cnt=2)]}, 0)


def test_systemview_task_id_reuse_does_not_consume_old_ready_time():
    data = {'version': '1.0', 'events': [event(10, 0, 1), event(6, .01, tid=7),
        event(29, .03, tid=7), event(8, .1, tid=7), event(4, .11, 1, 7), event(5, .2, 1)]}
    result = analyze_sysview(data, 0)
    assert result['ready_to_run'] == {}
    # One old lifetime is censored at deletion. The final STOP_EXEC leaves
    # a second, new ready interval censored by the end of this capture.
    assert result['censored_ready_intervals'] == 2


def test_systemview_preemption_wait_is_separate_from_blocking():
    data = {'version': '1.0', 'events': [event(10, 0, 1),
        event(4, .01, tid=7), event(5, .02), event(4, .02, tid=8),
        event(7, .04, tid=8), event(5, .04), event(4, .05, tid=7),
        event(17, .08), event(11, .1, 1)]}
    report = analyze_sysview(data, 0)
    assert report['ready_to_run']['7']['mean'] == pytest.approx(.03)
    assert '8' not in report['ready_to_run']


def test_systemview_trim_excludes_attach_edges_and_rejects_missing_coverage():
    data = {'version': '1.0', 'events': [event(10, 0), event(10, 0, 1),
        event(4, 1.1, tid=7), event(17, 1.1, 1), event(5, 2),
        event(17, 2.1), event(17, 2.1, 1), event(11, 3.2), event(11, 3.2, 1)]}
    report = analyze_sysview(data, 0, trim_seconds=1)
    assert report['scheduled_residency']['7'] == pytest.approx(.9)
    with pytest.raises(ProfileError, match='steady-state'):
        analyze_sysview(data, 0, trim_seconds=2)


def test_systemview_trim_preserves_running_context_from_before_the_window():
    data = {'version': '1.0', 'events': [event(10, 0), event(10, 0, 1),
        event(4, .5, tid=7), event(17, .5, 1), event(5, 2.5), event(17, 2.5, 1),
        event(11, 4), event(11, 4, 1)]}
    report = analyze_sysview(data, 0, trim_seconds=1)
    assert report['scheduled_residency'].get('7', 0) == pytest.approx(1.5)
    assert report['core_span_seconds'] == {'0': 2, '1': 2}


def test_pc_unresolved_addresses_and_missing_cores_are_visible():
    rows = [{'core': 0, 'pc': 1, 'host_before_ns': 1, 'host_after_ns': 2},
            {'core': 1, 'pc': 2, 'host_before_ns': 3, 'host_after_ns': 4}]
    report = analyze_pc(rows, {1: {'function': 'audio'}})
    assert report['cores']['1']['unresolved_samples'] == 1
    assert report['cores']['1']['functions'] == {'??': 1.0}
    with pytest.raises(ProfileError, match='Both cores'):
        analyze_pc(rows[:1], {})


def test_systemview_cleanup_and_failed_parser_cannot_be_presented_as_valid(tmp_path, monkeypatch):
    output = tmp_path / 'path with spaces'
    output.mkdir()
    idf = tmp_path / 'idf'
    parser = idf / 'tools/esp_app_trace/sysviewtrace_proc.py'
    parser.parent.mkdir(parents=True)
    parser.touch()
    python = tmp_path / 'idf-python'
    python.touch()
    logfile = tmp_path / 'openocd.log'
    logfile.touch()

    class TraceClient:
        def __init__(self):
            self.commands = []

        def running(self):
            pass

        def command(self, command):
            self.commands.append(command)
            reply = sysview_logger_reply(command, logfile)
            if reply is not None:
                return reply
            if command == 'esp sysview status':
                return 'Tracing is STOPPED. Size is 0 of -1'
            if command.startswith('esp sysview start'):
                for core in (0, 1):
                    (output / f'core{core}.SVDat').write_bytes(b'test trace')
            if command == 'esp sysview stop':
                return 'Tracing is STOPPED. Size is 20\nData: blocks incomplete 0, lost bytes: 0'
            return ''

    client = TraceClient()
    monkeypatch.setattr('tools.esp_profile.subprocess.run', lambda *a, **kw: SimpleNamespace(returncode=2))
    with pytest.raises(ProfileError, match='parser failed'):
        record_sysview(client, output, 5, python, idf, tmp_path / 'firmware.elf', 'xtensa-esp32s3-elf-',
                       openocd_log=logfile, clock=Clock())
    assert client.commands[-2:] == ['esp sysview stop', 'esp sysview status']
    assert (output / 'core0.SVDat').read_bytes() == b'test trace'
    metadata = json.loads((output / 'sysview-acquisition.json').read_text())
    assert metadata['parser_exit_code'] == 2
    assert 'hooks' in metadata['method']


@pytest.mark.parametrize('stats,final_state', [
    ('Data: blocks incomplete 1, lost bytes: 0', 'STOPPED'),
    ('Data: blocks incomplete 0, lost bytes: 17', 'STOPPED'),
    ('', 'STOPPED'),
    ('Data: blocks incomplete 0, lost bytes: 0', 'RUNNING'),
])
def test_sysview_transport_loss_or_unverified_stop_is_not_accepted(tmp_path, monkeypatch, stats, final_state):
    parser = tmp_path / 'tools/esp_app_trace/sysviewtrace_proc.py'
    parser.parent.mkdir(parents=True)
    parser.touch()
    python = tmp_path / 'idf-python'
    python.touch()
    logfile = tmp_path / 'openocd.log'
    logfile.touch()

    class TraceClient:
        def __init__(self):
            self.states = 0

        def running(self):
            pass

        def command(self, command):
            reply = sysview_logger_reply(command, logfile)
            if reply is not None:
                return reply
            if command == 'esp sysview status':
                self.states += 1
                return f'Tracing is {"STOPPED" if self.states == 1 else final_state}.'
            if command == 'esp sysview stop':
                return 'Tracing is STOPPED.\n' + stats
            if command.startswith('esp sysview start'):
                for core in (0, 1):
                    (tmp_path / f'core{core}.SVDat').write_bytes(b'retained trace')
            return ''

    def forbidden_parser(*args, **kwargs):
        pytest.fail('Transport must be qualified before invoking the parser')

    monkeypatch.setattr('tools.esp_profile.subprocess.run', forbidden_parser)
    with pytest.raises(ProfileError, match='transport loss or unverified final stop'):
        record_sysview(TraceClient(), tmp_path, 5, python, tmp_path,
                       tmp_path / 'firmware.elf', 'xtensa-esp32s3-elf-', openocd_log=logfile, clock=Clock())
    assert (tmp_path / 'core0.SVDat').read_bytes() == b'retained trace'
    assert stats in json.loads((tmp_path / 'sysview-transport.json').read_text())['stop_reply']


def test_comparison_requires_matching_workloads_and_independent_repeats():
    def run(share):
        return {'status': 'valid', 'capture_id': uuid.uuid4().hex,
                'contract': {'scenario': 'music', 'mode': 'pc'}, 'analysis': {
            'unit': 'share_of_valid_pc_samples',
            'cores': {c: {'functions': {'audio': share}} for c in ('0', '1')},
        }}
    report = compare([run(.3) for _ in range(5)], [run(.2) for _ in range(5)])
    assert report['repeated_windows']
    assert report['changes']['0']['audio']['delta'] == pytest.approx(-.1)
    assert report['changes']['0']['audio']['run_bootstrap_95_interval'] == pytest.approx([-.1, -.1])
    assert not compare([run(.3)], [run(.2)])['repeated_windows']
    bad = run(.2)
    bad['contract']['scenario'] = 'idle'
    with pytest.raises(ProfileError, match='identical'):
        compare([run(.3)], [bad])
    reused = run(.3)
    with pytest.raises(ProfileError, match='Independent'):
        compare([reused] * 5, [run(.2) for _ in range(5)])


@pytest.fixture
def sysview_log_rig(tmp_path, monkeypatch):
    output = tmp_path / 'capture'
    output.mkdir()
    parser = tmp_path / 'idf/tools/esp_app_trace/sysviewtrace_proc.py'
    parser.parent.mkdir(parents=True)
    parser.touch()
    python = tmp_path / 'idf-python'
    python.touch()
    logfile = tmp_path / 'openocd.log'
    logfile.write_text('Earlier attach: target halted\n')
    rig = SimpleNamespace(output=output, logfile=logfile, parser_calls=[],
                          active_text='', stop_text='', active_action=None, debug_level='2')

    def append(text):
        with logfile.open('a') as stream:
            stream.write(text + '\n')

    class TraceClient:
        reply_in_flight = False

        def __init__(self):
            self.commands = []

        def running(self):
            pass

        def command(self, command):
            self.commands.append(command)
            if command == 'debug_level':
                return rig.debug_level
            if command.startswith('echo {'):
                append(command[6:-1])
                return ''
            if command == 'esp sysview status':
                return 'Tracing is STOPPED.'
            if command.startswith('esp sysview start'):
                for core in (0, 1):
                    (output / f'core{core}.SVDat').write_bytes(b'real-file-fixture')
                append('Info : target halted, expected native attach\nInfo : target resumed')
            if command == 'esp sysview stop':
                append('Info : target halted, expected native detach\nInfo : target resumed')
                return 'Tracing is STOPPED.\nData: blocks incomplete 0, lost bytes: 0\n' + rig.stop_text
            return ''

    class LogClock(Clock):
        def sleep(self, seconds):
            super().sleep(seconds)
            append(rig.active_text)
            if rig.active_action:
                rig.active_action()

    rig.client = TraceClient()
    rig.clock = LogClock()
    def parse(*args, **kwargs):
        rig.parser_calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(esp_profile.subprocess, 'run', parse)

    def record():
        return record_sysview(rig.client, output, 5, python, parser.parents[2],
                              tmp_path / 'firmware.elf', 'xtensa-esp32s3-elf-', clock=rig.clock,
                              openocd_log=logfile)
    rig.record = record
    return rig


@pytest.mark.parametrize('location', ['stop_reply', 'async_log'])
def test_sysview_zero_loss_cannot_hide_CRC_halt_recovery(sysview_log_rig, location):
    rig = sysview_log_rig
    text = 'Warn : [5/5] CRC mismatch!\nAll CRC attempts failed, trying with halted target'
    if location == 'stop_reply':
        rig.stop_text = text
    else:
        rig.active_text = text
    with pytest.raises(ProfileError, match='CRC|recovery'):
        rig.record()
    assert 'esp sysview stop' in rig.client.commands
    assert not rig.parser_calls
    assert (rig.output / 'core0.SVDat').read_bytes() == b'real-file-fixture'


@pytest.mark.parametrize('text', [
    'Info : [esp32s3.cpu0] Target halted, debug_reason=00000001',
    'Info : [esp32s3.cpu1] Reset cause (12) - (Software CPU1 reset)',
    'Debug: target.c: target_halt() stopping a core',
    'Info : [esp32s3.cpu1] resumed',
])
def test_sysview_async_halt_reset_or_resume_rejects_active_window(sysview_log_rig, text):
    rig = sysview_log_rig
    rig.active_text = text
    with pytest.raises(ProfileError, match='active interval'):
        rig.record()
    assert 'esp sysview stop' in rig.client.commands
    assert not rig.parser_calls
    assert text in (rig.output / 'sysview-openocd.log').read_text()


@pytest.mark.parametrize('change', ['rotate', 'truncate', 'rewrite_prefix'])
def test_sysview_changed_log_identity_or_prefix_is_not_qualified(sysview_log_rig, change):
    rig = sysview_log_rig
    def alter():
        if change == 'rotate':
            rig.logfile.rename(rig.logfile.with_suffix('.old'))
            rig.logfile.write_text('replacement log\n')
        else:
            rig.logfile.write_text('' if change == 'truncate' else 'changed prefix\n' + 'x' * 200)
    rig.active_action = alter
    with pytest.raises(ProfileError, match='rotated|truncated|prefix changed'):
        rig.record()
    assert 'esp sysview stop' in rig.client.commands
    assert not rig.parser_calls
    transport = json.loads((rig.output / 'sysview-transport.json').read_text())
    assert transport['openocd_log']['status'] == 'invalid'


def test_sysview_native_attach_detach_halts_are_outside_accepted_window(sysview_log_rig):
    rig = sysview_log_rig
    metadata = rig.record()
    assert len(rig.parser_calls) == 1
    assert metadata['openocd_log']['status'] == 'qualified'
    assert metadata['openocd_log']['append_sha256'] == sha256(rig.output / 'sysview-openocd.log')
    assert 'expected native attach' in (rig.output / 'sysview-openocd.log').read_text()
    assert 'expected native detach' in (rig.output / 'sysview-openocd.log').read_text()


def test_sysview_missing_or_unbound_log_never_starts_trace(sysview_log_rig):
    rig = sysview_log_rig
    rig.logfile.unlink()
    with pytest.raises(FileNotFoundError):
        rig.record()
    assert not rig.client.commands
    rig.logfile.touch()
    original = rig.client.command
    rig.client.command = lambda command: '' if command.startswith('echo {') else original(command)
    with pytest.raises(ProfileError, match='not bound'):
        rig.record()
    assert not any(c.startswith('esp sysview start') for c in rig.client.commands)


def test_sysview_insufficient_log_verbosity_never_starts_trace(sysview_log_rig):
    rig = sysview_log_rig
    rig.debug_level = '1'
    with pytest.raises(ProfileError, match='INFO level'):
        rig.record()
    assert not any(c.startswith('esp sysview start') for c in rig.client.commands)


def test_sysview_debug_command_echo_does_not_duplicate_user_marker(sysview_log_rig):
    rig = sysview_log_rig
    rig.debug_level = '3'
    original = rig.client.command
    def verbose(command):
        if not command.startswith('echo {'):
            return original(command)
        marker = command[6:-1]
        with rig.logfile.open('a') as stream:
            stream.write(f'Debug: 7 20 command.c: handle_command() command - echo {marker}\n')
            stream.write(f'User : 8 21 command.c:617 handle_echo(): {marker}\n')
        return ''
    rig.client.command = verbose
    assert rig.record()['openocd_log']['status'] == 'qualified'


def test_sysview_stop_failure_does_not_replace_primary_acquisition_error(sysview_log_rig):
    rig = sysview_log_rig
    primary = ProfileError('primary workload abort')
    def abort():
        raise primary
    rig.active_action = abort
    original = rig.client.command
    def stop_fails(command):
        if command == 'esp sysview stop':
            rig.client.commands.append(command)
            raise ProfileError('secondary stop failure')
        return original(command)
    rig.client.command = stop_fails
    with pytest.raises(ProfileError, match='primary workload abort') as failure:
        rig.record()
    assert failure.value is primary
    assert 'secondary stop failure' in failure.value.cleanup_errors
    assert 'esp sysview stop' in rig.client.commands
    assert 'secondary stop failure' in (rig.output / 'sysview-transport.json').read_text()


def test_sysview_marker_failure_still_attempts_stop_on_synchronized_transport(sysview_log_rig):
    rig = sysview_log_rig
    original = rig.client.command
    def end_fails(command):
        if command.startswith('echo {') and command.endswith('_END}'):
            raise ProfileError('END marker failure')
        return original(command)
    rig.client.command = end_fails
    with pytest.raises(ProfileError, match='END marker failure'):
        rig.record()
    assert 'esp sysview stop' in rig.client.commands
    assert not rig.parser_calls


def test_systemview_analyze_rejects_legacy_or_tampered_log_receipt(tmp_path, monkeypatch, capsys):
    report = {'status': 'valid', 'mode': 'sysview', 'acquisition': {}}
    (tmp_path / 'capture.json').write_text(json.dumps(report))
    monkeypatch.setattr('sys.argv', ['esp_profile', 'analyze', str(tmp_path)])
    for metadata in ({}, {'status': 'qualified', 'append_file': 'sysview-openocd.log', 'append_sha256': '0' * 64}):
        report['acquisition']['openocd_log'] = metadata
        (tmp_path / 'capture.json').write_text(json.dumps(report))
        (tmp_path / 'sysview-openocd.log').write_text('altered log')
        with pytest.raises(SystemExit) as failure:
            esp_profile.main()
        assert failure.value.code == 2
        assert 'hash-bound' in capsys.readouterr().err
    assert not (tmp_path / 'analysis.json').exists()


@pytest.mark.parametrize('mode', ['pc', 'pmu'])
def test_external_sysview_json_cannot_bypass_log_gate_with_another_capture_mode(tmp_path, monkeypatch, mode):
    (tmp_path / 'capture.json').write_text(json.dumps({'status': 'valid', 'mode': mode}))
    external = tmp_path / 'external.json'
    external.write_text('{}')
    monkeypatch.setattr('sys.argv', ['esp_profile', 'analyze', str(tmp_path), '--sysview-json', str(external)])
    with pytest.raises(SystemExit) as failure:
        esp_profile.main()
    assert failure.value.code == 2
    assert not (tmp_path / 'analysis.json').exists()
