from aioesphomeapi import BinarySensorInfo, BinarySensorState, MediaPlayerInfo, MediaPlayerEntityState, MediaPlayerState

from tools.esp_device_observer import entity_identity, entity_index


def test_domain_and_subdevice_prevent_state_key_collision():
    binary = BinarySensorInfo(key=7, object_id='speaker')
    player = MediaPlayerInfo(key=7, object_id='speaker')
    nested = MediaPlayerInfo(key=7, object_id='speaker', device_id=1)
    index = entity_index([binary, player, nested])
    assert len(index) == 3
    reports = [BinarySensorState(key=7, state=True),
               MediaPlayerEntityState(key=7, state=MediaPlayerState.PLAYING),
               MediaPlayerEntityState(key=7, device_id=1, state=MediaPlayerState.IDLE)]
    states = {entity_identity(state): state for state in reports}
    assert states[entity_identity(player)].state is MediaPlayerState.PLAYING
    assert index[entity_identity(reports[0])] is binary
    assert index[entity_identity(reports[2])] is nested


def test_diagnostics_preserve_digit_fields_and_require_empty_output():
    from tools.esp_device_observer import AudioDiagnosticReader
    reader = AudioDiagnosticReader()
    reader.feed('[I][audio_stack.diagnostics:1]: BEGIN v=1 failed=NO transition=NO')
    reader.feed('[I][audio_stack.diagnostics:2]: speaker=NO i2s_error=NO mic2=2 speaker_queued_bytes=10816 pending_tx=0')
    reader.feed('[I][audio_stack.diagnostics:3]: END v=1 uptime_ms=20')
    assert reader.latest['i2s_error'] == 'NO'
    assert reader.latest['mic2'] == '2'
    assert not reader.output_is_quiescent()
    reader.feed('[I][audio_stack.diagnostics:1]: BEGIN v=1 failed=NO transition=NO')
    reader.feed('[I][audio_stack.diagnostics:2]: speaker=NO i2s_error=NO speaker_queued_bytes=0 pending_tx=0')
    reader.feed('[I][audio_stack.diagnostics:3]: END v=1 uptime_ms=20')
    assert reader.output_is_quiescent()


def test_diagnostic_packages_do_not_mix_interleaved_reports():
    from tools.esp_device_observer import DiagnosticReader
    reader = DiagnosticReader('voip_stack')
    for message in (
        '[I][audio_stack.diagnostics:1]: BEGIN v=1 failed=YES',
        '[I][voip_stack.diagnostics:1]: BEGIN v=1 call_hash=abc state=in_call',
        '[I][audio_stack.diagnostics:2]: END v=1 uptime_ms=2',
        '[I][voip_stack.diagnostics:2]: tx_packets=225 rx_packets=201',
        '[I][voip_stack.diagnostics:3]: END v=1 uptime_ms=3',
    ):
        reader.feed(message)
    assert reader.sequence == 1
    assert reader.latest['call_hash'] == 'abc'
    assert reader.latest['tx_packets'] == '225'
    assert reader.latest['rx_packets'] == '201'
    assert 'failed' not in reader.latest


def _voip_report(tx, rx, *, video=True, video_tx=6475, audio_active='YES'):
    # Same section names/order as voip_diagnostics.cpp, including duplicate keys.
    lines = [
        '[I][voip_stack.diagnostics:065]: BEGIN v=1 uptime_ms=1409887 state=in_call call_hash=4ddae9d6 transition=NO failed=NO',
        '[I][voip_stack.diagnostics:069]: configured: transport=udp sip_port=5060 rtp_port=40000 capability=full_duplex registration=unsupported',
        '[I][voip_stack.diagnostics:075]: sip: running=YES peer_transport=udp invite_pending=NO terminal_pending=NO status=200 event=SIP_RESPONSE',
        f'[I][voip_stack.diagnostics:082]: rtp: running=YES remote_sip_port=5060 remote_rtp_port=40000 tx_packets={tx} rx_packets={rx} tx_bytes=6599496 rx_bytes=18430480',
    ]
    if video:
        lines.append(f'[I][voip_stack.diagnostics:095]: video: active=NO send=NO pending=NO tx_packets={video_tx} rx_packets=0 tx_frames=949 rx_frames=0')
    lines.extend([
        '[I][voip_stack.diagnostics:102]: queues: tx_depth=0 tx_drops=0 rx_depth=0 rx_drops=0',
        '[I][voip_stack.diagnostics:108]: rx_jitter: depth=0 capacity=16 missing=0 late=0 duplicates=0 drops=0 discarded_frames=0',
        f'[I][voip_stack.diagnostics:116]: audio: active={audio_active} buffers_psram=YES task_stacks_psram=YES audio_stacks_psram=YES route=direct',
        '[I][voip_stack.diagnostics:122]: END v=1 uptime_ms=1409887 untracked_metrics=not_available',
    ])
    return lines


def test_video_diagnostics_do_not_replace_real_audio_packet_progress():
    from tools.esp_device_observer import DiagnosticReader

    reader = DiagnosticReader('voip_stack')
    for line in _voip_report(19878, 19820):
        reader.feed(line)
    before = reader.latest
    assert before['tx_packets'] == '19878'
    assert before['rx_packets'] == '19820'
    assert before['video_tx_packets'] == '6475'
    assert before['video_rx_packets'] == '0'
    assert before['video_tx_frames'] == '949'
    assert before['video_rx_frames'] == '0'
    assert before['video_active'] == before['video_send'] == before['video_pending'] == 'NO'
    assert before['active'] == 'YES'
    assert before['tx_bytes'] == '6599496'
    assert before['route'] == 'direct'
    assert before['missing'] == before['rx_drops'] == '0'
    for line in _voip_report(26406, 26346):
        reader.feed(line)
    assert reader.sequence == 2
    assert int(reader.latest['tx_packets']) - int(before['tx_packets']) == 6528
    assert int(reader.latest['rx_packets']) - int(before['rx_packets']) == 6526
    assert reader.latest['video_tx_packets'] == before['video_tx_packets']


def test_audio_only_voip_report_preserves_legacy_fields_without_video_keys():
    from tools.esp_device_observer import DiagnosticReader

    reader = DiagnosticReader('voip_stack')
    for line in _voip_report(20, 30, video=False):
        reader.feed(line)
    assert reader.sequence == 1
    assert reader.latest['tx_packets'] == '20'
    assert reader.latest['rx_packets'] == '30'
    assert reader.latest['active'] == reader.latest['running'] == 'YES'
    assert not any(key.startswith('video_') for key in reader.latest)


def test_three_device_readers_keep_interleaved_video_and_audio_reports_separate():
    from tools.esp_device_observer import AudioDiagnosticReader, DiagnosticReader

    readers = {name: DiagnosticReader('voip_stack') for name in ('spotpear', 'box', 'freenove')}
    audio = AudioDiagnosticReader()
    reports = {
        'spotpear': _voip_report(11, 12, video=False),
        'box': _voip_report(21, 22, video_tx=31, audio_active='NO'),
        'freenove': _voip_report(41, 42, video_tx=51),
    }
    audio_lines = [
        '[I][audio_stack.diagnostics:1]: BEGIN v=1 failed=NO transition=NO',
        '[I][audio_stack.diagnostics:2]: speaker=NO i2s_error=NO speaker_queued_bytes=0 pending_tx=0 mic2=2',
        '[I][audio_stack.diagnostics:3]: END v=1 uptime_ms=20',
    ]
    for offset in range(max(map(len, reports.values()))):
        for name, lines in reports.items():
            if offset < len(lines):
                readers[name].feed(lines[offset])
                audio.feed(lines[offset])  # A different component tag must be ignored.
        if offset < len(audio_lines):
            audio.feed(audio_lines[offset])
            for reader in readers.values():
                reader.feed(audio_lines[offset])
    assert all(reader.sequence == 1 for reader in readers.values())
    assert readers['spotpear'].latest['tx_packets'] == '11'
    assert 'video_tx_packets' not in readers['spotpear'].latest
    assert readers['box'].latest['tx_packets'] == '21'
    assert readers['box'].latest['video_tx_packets'] == '31'
    assert readers['box'].latest['active'] == 'NO'
    assert readers['freenove'].latest['tx_packets'] == '41'
    assert readers['freenove'].latest['video_tx_packets'] == '51'
    assert readers['freenove'].latest['active'] == 'YES'
    assert audio.sequence == 1
    assert audio.latest == {'v': '1', 'failed': 'NO', 'transition': 'NO', 'speaker': 'NO',
                            'i2s_error': 'NO', 'speaker_queued_bytes': '0', 'pending_tx': '0',
                            'mic2': '2', 'uptime_ms': '20'}
    assert audio.output_is_quiescent()
