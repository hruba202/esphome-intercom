"""Compile the build adaptation against the old and new CSI format contracts."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "idf_components/esp_video_csi_compat/CMakeLists.txt"
# The unchanged esp_video 2.4.1 pre-v3 format-selection boundary. The complete
# real library is additionally compiled and exercised by the P4 firmware gate.
BOUNDARY = """#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 0, 0)
        /* Old IDF version: use sensor format as CSI input */
        input = 8;
#else
        input = 16;
#endif
"""


@pytest.mark.parametrize("revision", [0, 100, 300])
@pytest.mark.parametrize("version", [(5, 5, 5), (5, 5, 6), (6, 0, 0)])
def test_csi_adapter_preserves_vendor_and_one_source(tmp_path, revision, version):
    source_dir = tmp_path / "video/src/device"
    source_dir.mkdir(parents=True)
    encoded = version[0] * 65536 + version[1] * 256 + version[2]
    original = f"""#include <assert.h>
#define ESP_IDF_VERSION_VAL(a,b,c) ((a)*65536+(b)*256+(c))
#define ESP_IDF_VERSION {encoded}
int main(void) {{
 int input = 0;
{BOUNDARY}
 assert(input == {8 if version < (5, 5, 6) else 16});
 return 0;
}}
"""
    source = source_dir / "esp_video_csi_format.c"
    source.write_text(original)
    # Production rev3 takes a different, already correct upstream branch.
    enabled = revision < 300 and (5, 5, 6) <= version < (6, 0, 0)
    (tmp_path / "CMakeLists.txt").write_text(f"""
cmake_minimum_required(VERSION 3.20)
project(csi_contract LANGUAGES C)
set(CONFIG_IDF_TARGET_ESP32P4 ON)
set(CONFIG_ESP_VIDEO_ENABLE_MIPI_CSI_VIDEO_DEVICE ON)
set(CONFIG_ESP32P4_REV_MIN_FULL {revision})
set(IDF_VERSION_MAJOR {version[0]})
set(IDF_VERSION_MINOR {version[1]})
set(IDF_VERSION_PATCH {version[2]})
add_executable(probe video/src/device/esp_video_csi_format.c)
function(idf_component_register)
endfunction()
function(idf_component_get_property result component property)
 if(property STREQUAL "COMPONENT_DIR")
  set(${{result}} "${{CMAKE_CURRENT_SOURCE_DIR}}/video" PARENT_SCOPE)
 else()
  set(${{result}} probe PARENT_SCOPE)
 endif()
endfunction()
include("{ADAPTER}")
get_target_property(final_sources probe SOURCES)
list(LENGTH final_sources count)
if(NOT count EQUAL 1)
 message(FATAL_ERROR "The adapter added a second CSI format implementation")
endif()
""")
    build = tmp_path / "build"
    subprocess.run(["cmake", "-S", str(tmp_path), "-B", str(build)], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
    if revision < 300:
        subprocess.run([str(build / "probe")], check=True)
    assert source.read_text() == original
    assert (build / "esp_video_csi_format.c").exists() is enabled
