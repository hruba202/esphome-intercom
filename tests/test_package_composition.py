"""Validate feature removal through ESPHome's real package and schema pipeline."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from esphome import yaml_util
import pytest


ROOT = Path(__file__).resolve().parents[1]
FULL = ['voice', 'wake_word', 'voip', 'music', 'sendspin', 'timers', 'status_led', 'diagnostics']


def test_full_lite_shipped_defaults_keep_sendspin_optional() -> None:
    from tests.native_profile import profile

    document = profile('full-experience/single-bus/generic-s3-full-lite-aec.yaml')
    assert 'sendspin' not in document, 'The 4 MB Lite default must not instantiate Sendspin'
    assert document['esp32']['flash_size'] == '4MB'
    assert 'voice_assistant' in document
    assert 'micro_wake_word' in document
    assert 'voip_stack' in document
    player = document['media_player'][0]
    assert 'media_pipeline' in player
    assert 'announcement_pipeline' in player


def device_config(directory: Path, features: list[str], profile_path: str = 'yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml') -> Path:
    profile = yaml_util.load_yaml(ROOT / profile_path)
    substitutions = dict(profile['substitutions'])
    substitutions['firmware_features'] = features
    substitutions['assets_base'] = str(ROOT) + '/'
    for key, repository in (
        ('ext_components_source', ROOT),
        ('audio_stack_components_source', Path(os.environ.get('AUDIO_STACK_REPO', ROOT.parent / 'esphome-audio-stack'))),
        ('voip_stack_components_source', Path(os.environ.get('VOIP_STACK_REPO', ROOT.parent / 'esphome-voip-stack'))),
        ('runtime_controller_components_source', Path(os.environ.get('RUNTIME_CONTROLLER_REPO', ROOT.parent / 'esphome-runtime-controller'))),
        ('camera_components_source', Path(os.environ.get('ESP_VIDEO_CAMERA_REPO', ROOT.parent / 'esphome-esp-video-camera'))),
    ):
        component_path = 'components' if key == 'camera_components_source' else 'esphome/components'
        substitutions[key] = str(repository / component_path)
    directory.mkdir(parents=True, exist_ok=True)
    for companion in (ROOT / profile_path).parent.glob('*.csv'):
        shutil.copy2(companion, directory / companion.name)
    (directory / 'secrets.yaml').write_text('wifi_ssid: fixture-network\nwifi_password: fixture-password\n')
    config = directory / 'device.yaml'
    package_path = str(profile['packages']['device']).split('/packages/', 1)[1].rsplit('@', 1)[0]
    config.write_text(yaml_util.dump({'substitutions': substitutions}) +
                      '\npackages:\n  device: !include ' +
                      str(ROOT / 'packages' / package_path) + '\n')
    return config


def maintenance_begin_actions(document):
    ota = next(item for item in document['ota'] if item['platform'] == 'esphome')
    for event, resume in (('on_begin', False), ('on_abort', True), ('on_error', True)):
        assert ota[event]['then'] == [{'script.execute': {'id': 'firmware_maintenance', 'resume': resume}}]
    owner = next(script for script in document['script'] if script['id'] == 'firmware_maintenance')
    assert owner['mode'] == 'restart'
    assert owner['parameters'] == {'resume': 'bool'}
    return owner['then'][0]['if']['else']


@pytest.mark.parametrize('features', [
    FULL,
    ['voip'],
    ['voice'],
    ['music'],
    ['voice', 'voip'],
    ['voice', 'wake_word', 'timers'],
    *[[feature for feature in FULL if feature != removed]
      for removed in ('timers', 'sendspin', 'wake_word', 'voip', 'music', 'voice', 'status_led')],
])
def test_ws3_native_feature_composition(tmp_path: Path, features: list[str]) -> None:
    config = device_config(tmp_path, features)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert document['substitutions']['firmware_features'] == features
    has_voice = 'voice' in features
    has_music = 'music' in features
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is has_voice
    assert ('micro_wake_word' in document) is (has_voice and 'wake_word' in features)
    assert ('sendspin' in document) is (has_music and 'sendspin' in features)
    assert ('light' in document) is ('status_led' in features)

    scripts = {str(script['id']) for script in document.get('script', [])}
    assert ('timer_alarm_loop' in scripts) is (has_voice and 'timers' in features)
    assert ('start_mww_when_ready' in scripts) is (has_voice and 'wake_word' in features)
    if 'voip' not in features:
        assert 'voip_start_ringtone' not in scripts
        assert not any(button.get('name') in ('Call', 'Next Contact', 'Previous Contact', 'Decline')
                       for button in document.get('button', []))
    players = document.get('media_player', [])
    assert len(players) <= 1
    if players:
        assert ('media_pipeline' in players[0]) is has_music
    speakers = [str(speaker.get('id')) for speaker in document['speaker']]
    inputs = int(has_music) + int(has_voice or 'voip' in features) + int('voip' in features)
    assert speakers.count('audio_mixer') == int(inputs >= 2)
    if inputs >= 2:
        mixer = next(speaker for speaker in document['speaker'] if speaker.get('id') == 'audio_mixer')
        expected_inputs = (
            (['media_mixer_input'] if has_music else [])
            + (['tts_mixer_input'] if has_voice or 'voip' in features else [])
            + (['voip_mixer_input'] if 'voip' in features else [])
        )
        assert [item['id'] for item in mixer['source_speakers']] == expected_inputs
    assert ('voip_speaker_input' in speakers) is ('voip' in features)
    assert ('media_speaker_input' in speakers) is has_music

    # Each input event is forwarded only once, even with combined features.
    if has_voice:
        starts = document['voice_assistant']['on_start']
        assert sum(action.get('runtime_controller.event', {}).get('event') == 'va_start'
                   for action in starts) == 1

    stops = maintenance_begin_actions(document)
    actions = [next(iter(action)) for action in stops]
    assert ('voice_assistant.stop' in actions) is has_voice
    assert ('micro_wake_word.stop' in actions) is (has_voice and 'wake_word' in features)
    assert ('voip_stack.stop' in actions) is ('voip' in features)
    assert actions[-2:] == ['esp_audio_stack.stop', 'wait_until']


def test_voice_preset_loads_outside_sibling_repository_layout(tmp_path: Path) -> None:
    from esphome.components.packages import resolve_packages

    target = tmp_path / 'download' / 'packages'
    shutil.copytree(ROOT / 'packages', target)
    config = tmp_path / 'voice.yaml'
    config.write_text('packages:\n  voice: !include ' + str(target / 'presets/voice_runtime.yaml') + '\n')
    resolved = resolve_packages(yaml_util.load_yaml(config))
    assert 'runtime_controller' in resolved
    assert 'voice_assistant' in resolved
    assert 'voip_stack' not in resolved


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/generic-s3-full-aec.yaml',
    'yamls/full-experience/dual-bus/generic-s3-full-aec.yaml',
    'yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml',
    'yamls/full-experience/esphome-native/generic-s3-full-esphome-native.yaml',
    'yamls/untested/generic-s3-full-afe.yaml',
])
@pytest.mark.parametrize('features', [FULL, ['voice'], ['voip'], ['music']])
def test_generic_feature_composition(tmp_path: Path, profile_path: str, features: list[str]) -> None:
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('features', [
    FULL + ['display'],
    FULL,
    *[[feature for feature in FULL if feature != removed] + ['display']
      for removed in ('voip', 'voice', 'music', 'sendspin', 'timers', 'wake_word')],
])
def test_spotpear_presentation_follows_features(tmp_path: Path, features: list[str]) -> None:
    config = device_config(tmp_path, features, 'yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml')
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert document['substitutions']['firmware_features'] == features
    assert ('lvgl' in document) is ('display' in features)
    assert ('display' in document) is ('display' in features)
    if 'display' in features:
        pages = {str(page['id']) for page in document['lvgl']['pages']}
        if 'voice' not in features:
            def events(value):
                if isinstance(value, dict):
                    event = value.get('runtime_controller.event', {})
                    if isinstance(event, dict) and 'event' in event:
                        yield event['event']
                    for child in value.values():
                        yield from events(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from events(child)
            assert not {'wake_word', 'manual_voice_toggle', 'voice_stop'} & set(events(document['lvgl']))
        assert ('call_idle_page' in pages) is ('voip' in features)
        assert ('timer_finished_page' in pages) is ('voice' in features and 'timers' in features)
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


def test_device_parameter_overrides_are_not_shadowed_or_concatenated(tmp_path: Path) -> None:
    config = device_config(tmp_path, FULL + ['display'], 'yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml')
    document = yaml_util.load_yaml(config)
    document['substitutions'].update({
        'voip_transport': 'tcp', 'voip_sip_port': '5090', 'voip_rtp_port': '41000',
        'mww_task_priority': '5', 'afe_mode_options': ['fd_low_cost', 'fd_high_perf'],
    })
    config.write_text(yaml_util.dump(document))
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    assert current['voip_stack']['transport'] == 'tcp'
    assert int(current['voip_stack']['sip_port']) == 5090
    assert int(current['voip_stack']['rtp_port']) == 41000
    mode = next(item for item in current['select'] if item['id'] == 'aec_mode_select')
    assert mode['options'] == ['fd_low_cost', 'fd_high_perf']
    boost = next(item for item in current['script'] if item['id'] == 'boost_mww_priority')
    body = boost['then'][-1]['lambda']
    assert 'vTaskPrioritySet(mww_task, 5);' in getattr(body, 'value', body)

    def mode_widgets(value):
        if isinstance(value, dict):
            if value.get('id') == 'ui_aec_mode_dd':
                yield value
            for child in value.values():
                yield from mode_widgets(child)
        elif isinstance(value, list):
            for child in value:
                yield from mode_widgets(child)

    widgets = list(mode_widgets(current['lvgl']))
    assert len(widgets) == 1
    assert widgets[0]['options'] == mode['options']


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
    'yamls/experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml',
])
@pytest.mark.parametrize('removed', [None, 'voip', 'voice', 'timers', 'display'])
def test_waveshare_v2_round_composition(tmp_path: Path, profile_path: str, removed: str | None) -> None:
    profile = yaml_util.load_yaml(ROOT / profile_path)
    features = [name for name in profile['substitutions']['firmware_features'] if name != removed]
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('processor', ['aec', 'afe'])
@pytest.mark.parametrize('removed', [None, 'display', 'sensor_dock'])
def test_box3_display_and_sensor_dock_are_independent(tmp_path: Path, processor: str, removed: str | None) -> None:
    profile_path = f'yamls/experimental/esp32-s3-box-3/esp32-s3-box-3-full-{processor}.yaml'
    profile = yaml_util.load_yaml(ROOT / profile_path)
    features = [name for name in profile['substitutions']['firmware_features'] if name != removed]
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    assert ('display' in current) is (removed != 'display')
    assert any(sensor.get('id') == 'radar_sensor' for sensor in current['binary_sensor']) is (removed != 'sensor_dock')
    assert any(bus.get('id') == 'external_i2c' for bus in current['i2c']) is (removed != 'sensor_dock')
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


@pytest.mark.parametrize('profile_path', [
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-full-duplex.yaml',
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-mic-only.yaml',
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-speaker-only.yaml',
    'yamls/voip-only/single-bus/generic-s3-voip.yaml',
    'yamls/voip-only/dual-bus/generic-s3-voip.yaml',
    'yamls/voip-only/single-bus/waveshare-s3-voip.yaml',
])
@pytest.mark.parametrize('features', [['voip', 'status_led', 'diagnostics'], ['voip'], [], ['status_led', 'diagnostics']])
def test_standalone_phone_has_no_implicit_led_or_coordinator(tmp_path, profile_path, features):
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    assert not {'runtime_controller', 'voice_assistant', 'micro_wake_word', 'sendspin'} & document.keys()
    assert ('light' in document) is ('status_led' in features)
    assert ('microphone' in document) is ('speaker-only' not in profile_path)
    assert ('speaker' in document) is ('mic-only' not in profile_path)
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
    'yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
])
@pytest.mark.parametrize('features', [
    [x for x in FULL if x != 'status_led'] + ['display', 'video'],
    *[[x for x in FULL + ['display', 'video'] if x not in ('status_led', removed)]
      for removed in ('display', 'voip', 'voice', 'timers', 'wake_word', 'music', 'sendspin')],
])
@pytest.mark.timeout(240)
def test_p4_feature_composition(tmp_path, profile_path, features):
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr

    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is ('voice' in features)
    assert ('micro_wake_word' in document) is ('voice' in features and 'wake_word' in features)
    assert ('sendspin' in document) is ('music' in features and 'sendspin' in features)
    assert ('lvgl' in document) is ('display' in features)
    if len(features) >= 9 or 'display' not in features:
        generated = subprocess.run(
            [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
            capture_output=True, text=True, timeout=180,
        )
        assert generated.returncode == 0, generated.stdout + generated.stderr


def test_legacy_runtime_packages_resolve_to_the_canonical_bindings(tmp_path):
    """Emulate a remote cache, without requiring sibling-relative includes."""
    from esphome.components.packages import resolve_packages
    runtime = Path(os.environ.get('RUNTIME_CONTROLLER_REPO', ROOT.parent / 'esphome-runtime-controller'))
    legacy = tmp_path / 'download/runtime'
    shutil.copytree(runtime / 'packages', legacy)
    for package in legacy.rglob('*.yaml'):
        text = package.read_text()
        # Only substitute Git transport with an isolated local cache. Package
        # merge, action composition and IDs still use ESPHome's native parser.
        import re
        text = re.sub(r'github://n-IA-hane/esphome-intercom/(packages/[^\s@]+)@[^\s]+',
                      lambda m: '!include ' + str(ROOT / m[1]), text)
        assert 'github://n-IA-hane/esphome-intercom/packages/' not in text
        package.write_text(text)
    config = tmp_path / 'legacy.yaml'
    config.write_text('packages:\n  runtime: !include ' + str(legacy / 'runtime_controller/full_controller_no_led.yaml') + '\n')
    resolved = resolve_packages(yaml_util.load_yaml(config))
    scripts = [s for s in resolved['script'] if s['id'] == 'runtime_voice_transition']
    assert len(scripts) == 1
    assert yaml_util.dump(scripts).count('event: va_stop_complete') == 2
    assert resolved['runtime_controller']['observe']['media_player'] == 'speaker_media_player'
    assert 'runtime_voice_start' in {s['id'] for s in resolved['script']}


@pytest.mark.parametrize('removed', [None, 'display', 'voip', 'voice', 'timers', 'wake_word', 'music', 'sendspin'])
@pytest.mark.timeout(180)
def test_first_generation_round_feature_composition(tmp_path, removed):
    features = [f for f in FULL + ['display'] if f not in ('status_led', removed)]
    profile = 'yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml'
    config = device_config(tmp_path, features, profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert 'esp_aec' in document and 'esp_afe' not in document
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is ('voice' in features)
    assert ('lvgl' in document) is ('display' in features)


@pytest.mark.parametrize('profile_path', [
    'yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml',
    'yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml',
])
@pytest.mark.parametrize('features', [['voip', 'display', 'status_led', 'diagnostics'], ['voip'], []])
@pytest.mark.timeout(180)
def test_standalone_display_phone_feature_composition(tmp_path, profile_path, features):
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('codec', ['jpeg', 'h264'])
@pytest.mark.parametrize('features', [['voip', 'display', 'video'], ['voip', 'display'], ['voip'], []])
@pytest.mark.timeout(180)
def test_p4_standalone_phone_keeps_codec_and_display_optional(tmp_path, codec, features):
    profile = f'yamls/voip-only/single-bus/waveshare-p4-touch-videophone-{codec}.yaml'
    config = device_config(tmp_path, features, profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    generated = next(tmp_path.rglob('main.cpp')).read_text()
    assert ('voip_stack::VoipStack' in generated) is ('voip' in features)
    assert ('esp_video_camera::ESPVideoCamera' in generated) is ('video' in features)
    assert ('esp_h264_video_source::' in generated) is ('video' in features and codec == 'h264')
    assert ('esp_jpeg_video_source::' in generated) is ('video' in features and codec == 'jpeg')


@pytest.mark.parametrize('features', [['voip'], []])
@pytest.mark.timeout(240)
def test_voice_pe_overlay_preserves_the_official_audio_owner(tmp_path, features):
    config = device_config(tmp_path, features,
                           'yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml')
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=210)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    assert 'esp_audio_stack' not in document
    assert 'esp_aec' not in document and 'esp_afe' not in document
    assert 'voice_assistant' in document


def test_full_ota_stops_music_and_announcements_independently(tmp_path):
    config = device_config(tmp_path, FULL)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    commands = [action['media_player.stop'] for action in maintenance_begin_actions(document)
                if 'media_player.stop' in action]
    assert len(commands) == 2
    assert [command.get('announcement', False) for command in commands] == [True, False]


def test_local_audio_provider_requests_https_certificate_bundle(tmp_path):
    config = device_config(tmp_path, ['voice'])
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = '\n'.join(p.read_text() for p in tmp_path.rglob('sdkconfig*') if p.is_file())
    assert 'CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y' in generated
    assert 'CONFIG_ESP_TLS_SKIP_SERVER_CERT_VERIFY=y' not in generated


def test_p4_local_idf_dependency_has_one_owner(tmp_path):
    config = device_config(tmp_path, ['voip'],
                           'yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml')
    document = yaml_util.load_yaml(config)
    document['substitutions']['intercom_idf_root'] = str(ROOT / 'idf_components')
    document['substitutions']['intercom_idf_ref'] = 'dev'
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    ppa = [item for item in document['esp32']['framework']['components'] if item['name'] == 'esp_driver_ppa']
    assert ppa == [{'name': 'esp_driver_ppa', 'path': str(ROOT / 'idf_components/esp_driver_ppa')}]


def test_local_gmf_override_only_binds_the_selected_afe(tmp_path):
    config = device_config(tmp_path, ['voice'])
    document = yaml_util.load_yaml(config)
    source = tmp_path / 'gmf'
    source.mkdir()
    document['substitutions']['gmf_ai_audio_source'] = str(source)
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    assert yaml_util.load_yaml(expanded)['esp_afe']['gmf_source'] == str(source)
