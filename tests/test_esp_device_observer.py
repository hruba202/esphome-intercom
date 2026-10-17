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
