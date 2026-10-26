"""Maintenance cancels every configured playback buffer before shared I2S closes."""
import subprocess
import sys

import yaml

from tests.native_profile import PackageLoader, profile
from tests.test_package_composition import device_config, maintenance_begin_actions

import pytest


@pytest.mark.parametrize('path', [
    'full-experience/single-bus/waveshare-s3-full-afe.yaml',
    'experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml',
    'full-experience/single-bus/spotpear-ball-v2-full-afe.yaml',
    'voip-only/single-bus/generic-s3-voip.yaml',
    'voip-only/single-bus/spotpear-ball-v2-voip.yaml',
    'voip-only/single-bus/waveshare-s3-voip.yaml',
])
def test_maintenance_stops_buffer_owners_before_the_shared_output(path):
    assert_shutdown_ownership(profile(path))


def assert_shutdown_ownership(document):
    parents = {}
    for speaker in document.get('speaker', []):
        if speaker['platform'] == 'mixer':
            for source in speaker['source_speakers']:
                parents[source['id']] = speaker['output_speaker']
        elif 'output_speaker' in speaker:
            parents[speaker['id']] = speaker['output_speaker']
    inputs = set()
    pipelines = [player[key]['speaker'] for player in document.get('media_player', [])
                 for key in ('announcement_pipeline', 'media_pipeline') if key in player]
    for source in pipelines:
        node = source
        visited = set()
        while node in parents and node not in visited:
            visited.add(node)
            node = parents[node]
        if node == 'hw_speaker':
            inputs.update(visited)
    actions = maintenance_begin_actions(document)
    stops = [action['speaker.stop'] for action in actions if 'speaker.stop' in action]
    stops = [stop['id'] if isinstance(stop, dict) else stop for stop in stops]
    hardware = stops.index('hw_speaker')
    assert inputs <= set(stops[:hardware]), (
        'Playback buffers have no stop intent before shared hardware shutdown',
        sorted(inputs - set(stops[:hardware])), stops,
    )
    # Follow the configured graph, rather than baking the standard input IDs
    # into the test. A direct resampler has no mixer to stop.
    for source in inputs:
        parent = parents[source]
        assert stops.index(source) < stops.index(parent)


@pytest.mark.parametrize('features', [['voice'], ['music'], ['voice', 'music'], []])
def test_single_and_absent_consumers_have_no_unused_shutdown_bindings(tmp_path, features):
    config = device_config(tmp_path, features)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_shutdown_ownership(yaml.load(result.stdout, Loader=PackageLoader))
