"""Run the real diagnostic against independently modelled DMA and CPU views."""

from pathlib import Path
import resource
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "esphome/components/p4_framebuffer_capture/p4_framebuffer_capture.cpp"


def _no_core_dump():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@pytest.mark.parametrize("sync_error", [False, True])
def test_snapshot_reads_dma_memory_and_does_not_write_back_stale_cache(tmp_path, sync_error):
    headers = {
        "esphome/core/component.h": "#pragma once\nnamespace esphome {class Component {public: virtual void setup(){}; void mark_failed(){};};}\n",
        "esphome/core/log.h": "#pragma once\n#define ESP_LOGE(...)\n#define ESP_LOGW(...)\n#define ESP_LOGI(...)\n",
        "esphome/components/mipi_dsi/mipi_dsi.h": """#pragma once
#include <cstddef>
#include <cstdint>
namespace esphome::mipi_dsi {struct MipiDsi {
 uint16_t cache[16]{}, memory[16]{};
 uint8_t *get_frame_buffer() {return reinterpret_cast<uint8_t*>(cache);}
 size_t get_frame_buffer_size() {return sizeof(cache);}
 int get_width_internal(){return 4;} int get_height_internal(){return 4;}
};}
""",
        "esp_heap_caps.h": "#pragma once\n#include <cstdlib>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\ninline void* heap_caps_malloc(size_t s,int){return std::calloc(1,s);}\n",
        "esp_cache.h": "#pragma once\n#include <cstddef>\n#define ESP_OK 0\n#define ESP_CACHE_MSYNC_FLAG_DIR_M2C 8\nint esp_cache_msync(void*,size_t,int);\n",
        "lwip/inet.h": "#include <arpa/inet.h>\n",
        "lwip/sockets.h": "#include <sys/socket.h>\n#include <unistd.h>\n",
        "freertos/FreeRTOS.h": "#pragma once\n#define pdPASS 1\n",
        "freertos/task.h": "#pragma once\nint xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,void*);\ninline void vTaskDelete(void*){}\n",
    }
    for name, text in headers.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "main.cpp").write_text(f"""
#include <cassert>
#include <cstring>
#include "{SOURCE.parent / 'p4_framebuffer_capture.h'}"
#include "esp_cache.h"
static esphome::mipi_dsi::MipiDsi *screen;
static int sync_calls=0, task_calls=0;
int esp_cache_msync(void *address,size_t size,int flags) {{
 assert(address==screen->cache && size==sizeof(screen->cache));
 assert(flags==ESP_CACHE_MSYNC_FLAG_DIR_M2C); // Never overwrite DMA memory.
 sync_calls++;
 if ({int(sync_error)}) return 1;
 std::memcpy(screen->cache,screen->memory,size); return ESP_OK;
}}
int xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,void*) {{
 task_calls++; return 1; // Network sending is outside this cache contract.
}}
struct Probe:esphome::p4_framebuffer_capture::P4FramebufferCapture {{
 using P4FramebufferCapture::snapshot_; using P4FramebufferCapture::capture_active_;
}};
int main() {{
 esphome::mipi_dsi::MipiDsi display;screen=&display;
 for(int i=0;i<16;i++) display.memory[i]=0x1234+i;
 Probe probe;probe.set_display(&display);probe.set_downsample(2);probe.setup();probe.capture();
 if ({int(sync_error)}) {{
  assert(task_calls==0 && !probe.capture_active_.load());
 }} else {{
  auto *pixels=reinterpret_cast<uint16_t*>(probe.snapshot_);
  assert(pixels[0]==0x1234 && pixels[1]==0x1236 && pixels[2]==0x123c && pixels[3]==0x123e);
  assert(task_calls==1);
 }}
 assert(sync_calls==1);
 for(int i=0;i<16;i++) assert(display.memory[i]==0x1234+i);
 std::free(probe.snapshot_);
}}
""")
    executable = tmp_path / "probe"
    subprocess.run(["g++", "-std=c++17", "-I", str(tmp_path), str(SOURCE),
                    str(tmp_path / "main.cpp"), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, preexec_fn=_no_core_dump)
