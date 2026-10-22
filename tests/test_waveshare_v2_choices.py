"""Same-board codec calibration and native package choices for Waveshare V2."""
from __future__ import annotations

import math

import pytest

from tests.native_profile import profile

LEGACY_PROFILES = {
    'aec': 'experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml',
    'afe': 'full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
}


@pytest.mark.parametrize('backend', ['aec', 'afe'])
def test_v2_codec_calibration_belongs_to_board_not_processor(backend: str) -> None:
    document = profile(LEGACY_PROFILES[backend])
    audio = document['esp_audio_stack']
    codec = audio['codec']
    # NS4150B Av=240k/Rin with the V2 board's 100k input resistors.
    assert codec['output'].get('pa_gain', 0.0) == pytest.approx(20 * math.log10(2.4), abs=0.01)
    # Both DSP choices receive the same unclipped analog microphone input and
    # independently amplified MIC3 playback reference (serial TDM slot 1).
    assert codec['input']['gain_db'] == 0.0
    assert codec['input']['ref_channel'] == 2
    assert codec['input']['ref_gain_db'] == 24.0
    assert audio['tdm_ref_slot'] == 1
    assert audio['sample_rate'] == 48000
    assert audio['output_sample_rate'] == 16000


PUBLIC = 'yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml'


def _config(directory, **choices):
    from esphome import yaml_util
    from tests.test_package_composition import device_config

    config = device_config(directory, ['voice', 'wake_word', 'voip', 'music', 'sendspin', 'timers', 'display', 'diagnostics'], PUBLIC)
    document = yaml_util.load_yaml(config)
    document['substitutions'].update(choices)
    config.write_text(yaml_util.dump(document))
    return config


def _expand(config):
    from pathlib import Path
    import subprocess
    import sys

    from esphome import yaml_util

    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr
    output = Path(config).with_name('expanded.yaml')
    output.write_text(result.stdout)
    return yaml_util.load_yaml(output)


@pytest.mark.parametrize('backend', ['aec', 'afe'])
@pytest.mark.parametrize('mix', ['passthrough', 'average'])
@pytest.mark.parametrize('transport', ['udp', 'tcp'])
def test_v2_native_choices_bind_one_processor_and_same_consumer(tmp_path, backend, mix, transport):
    document = _expand(_config(tmp_path, audio_processor=backend, mic_mix=mix,
                               voip_transport=transport, mic_id='custom_processed_mic'))
    assert ('esp_aec' in document) is (backend == 'aec')
    assert ('esp_afe' in document) is (backend == 'afe')
    assert ('audio_dsp_iram' in document) is (backend == 'afe')
    audio = document['esp_audio_stack']
    assert audio['mic_mix'] == mix
    assert audio['tdm_mic_slots'] == ([0, 2] if backend == 'afe' or mix == 'average' else [0])
    assert str(audio['processor_id']) == backend + '_processor'
    assert document['voip_stack']['transport'] == transport
    assert document['esp32']['framework']['advanced']['compiler_optimization'] == (
        'SIZE' if backend == 'afe' else 'PERF'
    )
    for consumer in ('voice_assistant', 'micro_wake_word', 'voip_stack'):
        assert 'custom_processed_mic' in str(document[consumer]['microphone'])
    assert str(document['microphone'][0]['id']) == 'custom_processed_mic'
    if backend == 'afe':
        processor = document['esp_afe']
        assert processor['mic_num'] == (1 if mix == 'average' else 2)
        assert processor['se_enabled'] is (mix == 'passthrough')
        assert processor['output_prebuffer_frames'] == (0 if mix == 'average' else 1)
        assert processor['agc_enabled'] is False
        agc = [item['agc'] for item in document['switch'] if 'agc' in item]
        assert bool(agc) is (mix == 'average')
        if agc:
            assert agc[0]['restore_mode'] == 'RESTORE_DEFAULT_OFF'
    components = {component for provider in document['external_components']
                  for component in provider.get('components', [])}
    assert ('esp_afe' in components) is (backend == 'afe')
    assert ('esp_aec' in components) is (backend == 'aec')
    # Hardware calibration survives every choice rather than following a DSP.
    assert audio['codec']['input']['gain_db'] == 0.0
    assert audio['codec']['input']['ref_gain_db'] == 24.0
    assert audio['codec']['output']['pa_gain'] == 7.6


@pytest.mark.parametrize('choice,value', [('audio_processor', 'afee'), ('mic_mix', 'stereo-ish'), ('voip_transport', 'sctp')])
def test_v2_invalid_choices_fail_validation(tmp_path, choice, value):
    import subprocess
    import sys

    config = _config(tmp_path, **{choice: value})
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode != 0
    assert value in result.stdout + result.stderr


def test_v2_uppercase_choices_resolve_same_backend(tmp_path):
    document = _expand(_config(tmp_path, audio_processor='AFE', mic_mix='AVERAGE', voip_transport='TCP'))
    assert 'esp_aec' not in document and document['esp_afe']['mic_num'] == 1
    # `config --no-defaults` preserves scalar spelling; codegen receives the
    # validated lower-case enum from the VoIP component's existing schema.
    assert str(document['voip_stack']['transport']).lower() == 'tcp'
