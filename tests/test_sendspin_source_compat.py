"""Run the actual CMake adapter against pinned upstream source generations."""
from pathlib import Path
import os
import subprocess
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = (
    '30514d5102c269a0c7fa6a13932d6bf7f2ae1abc',  # 0.7.2
    '91038b72892596b9ec0cccfec1b9e6ebc58ba148',  # 0.9.3
    'b95cf854a784ed308b1f8fe9697f414b02768ddd',  # 0.9.5
)


@pytest.mark.parametrize('revision', REVISIONS)
def test_known_sendspin_sources_keep_prefix_trimming(tmp_path, revision):
    sources = tmp_path / 'library/src'
    sources.mkdir(parents=True)
    repo = os.environ.get('SENDSPIN_REPO')
    for role in ('sync_task', 'artwork_role', 'visualizer_role'):
        relative = f'src/{role}.cpp'
        if repo:
            data = subprocess.check_output(['git', '-C', repo, 'show', f'{revision}:{relative}'])
        else:
            with urllib.request.urlopen(f'https://raw.githubusercontent.com/Sendspin/sendspin-cpp/{revision}/{relative}', timeout=30) as response:
                data = response.read()
        (sources / f'{role}.cpp').write_bytes(data)
    cmake = tmp_path / 'CMakeLists.txt'
    cmake.write_text(f'''cmake_minimum_required(VERSION 3.16)
project(sendspin_adapter LANGUAGES CXX)
add_library(sendspin STATIC "{sources}/sync_task.cpp" "{sources}/artwork_role.cpp" "{sources}/visualizer_role.cpp")
function(idf_component_register)
endfunction()
function(idf_component_get_property output component property)
  if(property STREQUAL "COMPONENT_LIB")
    set(${{output}} sendspin PARENT_SCOPE)
  else()
    set(${{output}} "{sources.parent}" PARENT_SCOPE)
  endif()
endfunction()
add_subdirectory("{ROOT}/esphome/components/sendspin_compat/voip_sendspin_compat" adapter)
''')
    build = tmp_path / 'build'
    result = subprocess.run(['cmake', '-S', str(tmp_path), '-B', str(build)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    sync = (build / 'adapter/sync_task_compat.cpp').read_text()
    assert 'client_timestamp < sync_context.new_audio_client_playtime - HARD_SYNC_THRESHOLD_US' not in sync
    # The later decoded-frame trimming must remain: removing all late-data handling is not the fix.
    assert 'late_frames' in sync and 'actual_drop' in sync
    for role in ('sync_task', 'artwork_role', 'visualizer_role'):
        generated = (build / f'adapter/{role}_compat.cpp').read_text()
        assert 'PlatformThreadConfig thread_config(' in generated
        assert 'if (!thread_config.valid())' in generated
    # A changed upstream contract must stop the build rather than silently accepting unreviewed code.
    original = sources / 'sync_task.cpp'
    original.write_text(original.read_text().replace('HARD_SYNC_THRESHOLD_US', 'UNKNOWN_THRESHOLD'))
    rejected = subprocess.run(['cmake', '-S', str(tmp_path), '-B', str(build)], capture_output=True, text=True)
    assert rejected.returncode != 0
    assert 'requires review' in rejected.stderr
