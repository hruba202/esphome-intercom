"""Use ESPHome's real parser to validate user-facing feature selection."""
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('profile', [
    'yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml',
    'yamls/voip-only/single-bus/waveshare-s3-voip.yaml',
    'yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml',
])
def test_unknown_firmware_feature_is_a_configuration_error(tmp_path, profile):
    config = device_config(tmp_path, ['voiip'], profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode != 0, 'Unknown feature voiip silently produced a valid firmware configuration'
    assert 'voiip' in result.stdout + result.stderr
    assert 'firmware_feature' in result.stdout + result.stderr


@pytest.mark.parametrize('feature,required,absent_component', [
    ('sendspin', 'music', 'sendspin'),
    ('wake_word', 'voice', 'micro_wake_word'),
    ('timers', 'voice', 'voice_assistant'),
])
def test_missing_feature_dependencies_are_visible(tmp_path, feature, required, absent_component):
    from esphome import yaml_util
    config = device_config(tmp_path, [feature])
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"'{feature}' is disabled: requires {required}" in result.stderr
    path = tmp_path / 'expanded.yaml'
    path.write_text(result.stdout)
    assert absent_component not in yaml_util.load_yaml(path)
