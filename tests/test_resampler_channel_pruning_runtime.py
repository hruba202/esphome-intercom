"""Production wrapper + pinned ART/PCM/mixer/GainRamp, with host I/O adapters.

The host ring and sink model partial-byte I/O, not RTOS timing or ESP hardware.
No DSP algorithm is reproduced in the fixture. The DSP counter wraps the real
ANSI convolution routine selected by the library on a non-Xtensa host.
"""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import urllib.request

import esphome
import pytest

ROOT = Path(__file__).resolve().parents[1]
# Real ART/PCM/mixer sources plus ASan can exceed the global 30s setup budget
# on a contended CI worker. Compilers and each runtime have separate limits.
pytestmark = pytest.mark.timeout(120)
REVISION = 'e06373fa41529064cb852e2312a32a8148ddaaac'  # esp-audio-libs4.1.0
CHECKSUMS = {'include/resampler.h': '4452734faa32c0973f239f3c2c748ff3a05455b7fc7d0b8d61f20762186ff596', 'include/art_resampler.h': 'b2c2848ab436375fbb707b7f140aa8326485b0a9a44fb3ba209612b58dde00f9', 'include/art_biquad.h': '4bc23aa7a4af8abf9130e36fe1a33b3d11af35a4cf593d0611d937365aac6aff', 'include/pcm_convert.h': 'ee22923fe4e2a4043f95c858da57032b4f009395a42d373e0742dc0b6e1b7420', 'include/mixer.h': '4cc82d58b5919d3ecb46ee4f492d178ec7a83fc3765eda2e277388685301e019', 'include/gain.h': '91bd809286ba4907d9a27913fb68e1b2580bc2c5d0c71a89313deff1a0347a60', 'src/resample/resampler.cpp': '4f2447715bfdb2a8423ab518fd92ad9dabe8dc7130a5cece845f7fce9f46cf40', 'src/resample/art_resampler.cpp': '22565c68a226bd4b7a4056be27176e9753620f0968085e8ddff8dba0772264e9', 'src/resample/art_biquad.cpp': '8a3d0d3abdcb7ec0275b340cd392c93fe92a78a93946ed9f65f373eee06af543', 'src/quantization_utils.cpp': '55fde3047decde8aec65190be1409213e868ed04c78e3bc4250d3d513ee78933', 'src/quantization_utils.h': 'f6d4c238690f71f7e5ffbb0615c3fca7185c2f7cc8c33e15d527530f53503fa2', 'src/memory_utils.cpp': '24afa10e77b0186815803ea0f3097d1430b7baa842dfbd6348cf29bb932f7ea9', 'src/memory_utils.h': '26b62209e9fc1acfccb4f082f630f78551b791d0af91b0ec44d0fa0d42695f94', 'src/pcm_convert.cpp': 'bd05c0d9df4b4b35e5e0f9ff41a9137591905db28bc386c4405fe59bf90348f9', 'src/mixer.cpp': '71ec348fbe815baa4a81e9c08c03eb54963ffb27dc0ea0a5dbbed2ca7cda6ca7', 'src/gain.cpp': '421e549b8abe6f9fc636725c4412fb224436986971d8e6f84d3645ec4b607a68', 'src/compiler.h': '65a46977da2d707cf51c23863d5e941b938631ecbb34b947435abc8c0ded3d1d', 'src/q31_utils.h': '847f3cb2afcd7c6d5ca789088fdcc13e6223c63844d837a6b411ebad13e30088', 'src/dsp/dsp.h': '039cc5fa81ba159a5f6f887c7cbe8c5bc1658289c3b1f0b115dd14b707aade77', 'src/dsp/dsp_platform.h': 'b15e6c7c86e479ffed045875b0a5634ef6e2b090dbb4813241a6a218554dfe16', 'src/dsp/dsps_dotprod_f32_ansi.c': '7d1c4aaca2c381ef2133501d706b75e253ebc14c56bdce812240a53a165ea056'}


def _companion_facade():
    companion = Path(os.environ.get('AUDIO_STACK_REPO', ROOT.parent / 'esphome-audio-stack'))
    source = companion / 'esphome/components/resampler/speaker/resampler_speaker.cpp'
    assert source.is_file(), (
        'Run scripts/coordinated_sources.py --root .. --ref dev or set AUDIO_STACK_REPO '
        'to the coordinated Audio Stack checkout; the software CI records its SHA in coordinated-sources.json.'
    )
    return source


@pytest.fixture(scope='module')
def pruning_binary(tmp_path_factory):
    folder = tmp_path_factory.mktemp('resampler-pruning')
    dependency = Path(os.environ.get('AUDIO_LIBS_REPO', ROOT / '.test-deps/esp-audio-libs'))
    for relative, expected in CHECKSUMS.items():
        path = dependency / relative
        if not path.exists():
            url = f'https://raw.githubusercontent.com/esphome-libs/esp-audio-libs/{REVISION}/{relative}'
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            assert hashlib.sha256(data).hexdigest() == expected, relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative
    stubs = {
        'esphome/core/defines.h': '#pragma once\n',
        'freertos/FreeRTOS.h': '#pragma once\nusing TickType_t=unsigned;\n#define pdMS_TO_TICKS(x) (x)\n',
        'esp_err.h': '#pragma once\nusing esp_err_t=int;\nconstexpr int ESP_OK=0, ESP_ERR_NO_MEM=1, ESP_ERR_NOT_SUPPORTED=2, ESP_ERR_INVALID_STATE=3;\n',
        'esphome/core/hal.h': '#pragma once\ninline void delay(unsigned){}\n',
        'esphome/core/helpers.h': r'''#pragma once
#include <algorithm>
#include <array>
#include <cstdlib>
#include <memory>
#include <cstring>
#include <strings.h>
#define ESPDEPRECATED(message, version)
extern unsigned ram_allocations;
namespace esphome {
using std::make_unique;
template<class T> struct RAMAllocator {
 T *allocate(size_t n) {++ram_allocations;return static_cast<T*>(std::malloc(n*sizeof(T)));}
 T *reallocate(T*p,size_t n){++ram_allocations;return static_cast<T*>(std::realloc(p,n*sizeof(T)));}
 void deallocate(T*p,size_t){std::free(p);}
};
inline bool str_endswith_ignore_case(const char*s,const char*tail){
 size_t n=std::strlen(s),k=std::strlen(tail);return n>=k && strcasecmp(s+n-k,tail)==0;
}
}
''',
        'esphome/components/ring_buffer/ring_buffer.h': r'''#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>
#include "freertos/FreeRTOS.h"
namespace esphome::ring_buffer {
class RingBuffer {
 public:
 std::vector<uint8_t> bytes;size_t cursor=0,read_limit=512;bool acquired=false;
 std::array<uint8_t,128> storage{};unsigned reused_exposures=0;size_t previous_length=0;
 size_t available()const{return bytes.size()-cursor;}
 void reset(){bytes.clear();cursor=0;acquired=false;}
 size_t write_without_replacement(const void*p,size_t n,TickType_t){
  auto*b=static_cast<const uint8_t*>(p);bytes.insert(bytes.end(),b,b+n);return n;
 }
 size_t read(void*p,size_t n,TickType_t){n=std::min(n,available());std::memcpy(p,bytes.data()+cursor,n);cursor+=n;return n;}
 void*receive_acquire(size_t&n,size_t limit,TickType_t){
  if(acquired||!available())return nullptr;n=std::min({available(),limit,read_limit,storage.size()});
  std::memcpy(storage.data(),bytes.data()+cursor,n);cursor+=n;acquired=true;
  if(previous_length==n)++reused_exposures;previous_length=n;return storage.data();
 }
 void receive_release(void*){acquired=false;}
};
}
''',
        'esphome/components/speaker/speaker.h': r'''#pragma once
#include <cstddef>
#include <cstdint>
#include "freertos/FreeRTOS.h"
namespace esphome::speaker {
class Speaker {
 public:
 virtual ~Speaker()=default;
 virtual size_t play(const uint8_t*,size_t,TickType_t)=0;
 virtual bool has_buffered_data()const{return false;}
 virtual void stop(){}
};
}
''',
    }
    for relative, text in stubs.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    mixer = (ROOT / 'esphome/components/mixer/speaker/mixer_speaker.cpp').read_text()
    start = mixer.index('size_t SourceSpeaker::process_data_from_source(')
    end = mixer.index('\nvoid SourceSpeaker::enter_stopping_state_', start)
    code = (ROOT / 'tests/fixtures/resampler_pruning_main.cpp').read_text().replace(
        '// @PRODUCTION_DUCK_METHODS@', mixer[start:end])
    cpp = folder / 'main.cpp'
    cpp.write_text(code)
    audio = ROOT / 'esphome/components/audio'
    binary = folder / 'pruning'
    wrapper = Path(os.environ.get('AUDIO_RESAMPLER_SOURCE', audio))
    sources = [audio / name for name in ('audio.cpp', 'audio_transfer_buffer.cpp')]
    sources.append(wrapper / 'audio_resampler.cpp')
    sources += [dependency / name for name in (
        'src/resample/resampler.cpp', 'src/resample/art_resampler.cpp', 'src/resample/art_biquad.cpp',
        'src/quantization_utils.cpp', 'src/memory_utils.cpp', 'src/pcm_convert.cpp',
        'src/mixer.cpp', 'src/gain.cpp', 'src/dsp/dsps_dotprod_f32_ansi.c')]
    command = ['g++', '-std=c++20', '-O1', '-g', '-DUSE_ESP32', '-DUSE_SPEAKER',
               '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
               '-I', str(folder), '-I', str(wrapper), '-I', str(audio), '-I', str(dependency / 'include'),
               str(cpp), *map(str, sources), '-Wl,--wrap=dsps_dotprod_f32_ansi',
               '-Wl,--wrap=_ZN14esp_audio_libs11pcm_convert11copy_framesEPKhPhhhhhj',
               '-o', str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize('rate', [16000, 44100, 48000])
@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize(('taps', 'filters'), [(16, 16), (32, 32), (64, 8)])
def test_unused_right_channel_preserves_pcm_credit_and_ducking(
    pruning_binary, rate, partial, channels, taps, filters
):
    result = subprocess.run([str(pruning_binary), str(rate), str(int(partial)), str(channels),
                             str(taps), str(filters)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_facade_call_compiles_with_the_actual_official_four_argument_header(pruning_binary):
    """The optional owned extension must not break standalone Audio Stack."""
    folder = pruning_binary.parent
    native = Path(esphome.__file__).parent
    header = native / 'components/audio/audio_resampler.h'
    assert 'ESPHOME_AUDIO_HAS_MONO_CHANNEL_PRUNING' not in header.read_text()
    facade = _companion_facade()
    source = facade.read_text()
    start = source.index('#ifdef ESPHOME_AUDIO_HAS_MONO_CHANNEL_PRUNING')
    end = source.index('\n\n    if (err == ESP_OK)', start)
    cpp = folder / 'native-header.cpp'
    cpp.write_text(f'#include "{header}"\n' + r'''
struct Self {
 esphome::audio::AudioStreamInfo audio_stream_info_,target_stream_info_;
 uint16_t taps_=16,filters_=16;bool mono_mixer_output_=true;
};
void verify(esphome::audio::AudioResampler *resampler,Self *this_resampler) {
''' + source[start:end] + '\n(void)err;\n}\n')
    library = Path(os.environ.get('AUDIO_LIBS_REPO', ROOT / '.test-deps/esp-audio-libs'))
    result = subprocess.run(['g++', '-std=c++20', '-DUSE_ESP32', '-DUSE_SPEAKER',
                             '-I', str(folder), '-I', str(library / 'include'),
                             '-fsyntax-only', str(cpp)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr


def test_facade_preserves_native_stereo_and_the_no_task_bypass(pruning_binary):
    """Exercise actual facade decisions, without simulating task scheduling."""
    folder = pruning_binary.parent
    source = _companion_facade().read_text()
    start = source.index('esp_err_t ResamplerSpeaker::start_()')
    end = source.index('\nvoid ResamplerSpeaker::stop()', start)
    bodies = source[start:end]
    start = source.index('bool ResamplerSpeaker::requires_resampling_() const')
    end = source.index('\nvoid ResamplerSpeaker::resample_task(', start)
    bodies += source[start:end]
    cpp = folder / 'facade-bypass.cpp'
    cpp.write_text(r'''
#include "audio_resampler.h"
#include <cassert>
constexpr int tskNO_AFFINITY=-1;
using namespace esphome;
namespace esphome::resampler {
struct Output {
 audio::AudioStreamInfo info;
 unsigned starts=0;
 void set_audio_stream_info(audio::AudioStreamInfo value){info=value;}
 void start(){++starts;}
};
struct Task {
 unsigned creates=0;
 template<class... T>bool create(T...){++creates;return true;}
};
struct ResamplerSpeaker {
 audio::AudioStreamInfo audio_stream_info_,target_stream_info_;
 Output *output_speaker_;
 Task task_;
 uint32_t target_sample_rate_=48000;
 uint8_t target_bits_per_sample_=16,task_priority_=1;
 int task_core_=-1;
 bool passthrough_bits_per_sample_=true,task_stack_in_psram_=false,mono_mixer_output_=true;
 static constexpr unsigned TASK_STACK_SIZE=3072;
 static void resample_task(void*){}
 esp_err_t start_();
 bool requires_resampling_()const;
};
''' + bodies + r'''
}
int main(){
 for(unsigned channels : {1,2}) {
  resampler::Output output;
  resampler::ResamplerSpeaker facade;
  facade.output_speaker_=&output;
  facade.audio_stream_info_=audio::AudioStreamInfo(16,channels,48000);
  assert(!facade.requires_resampling_() && facade.start_()==ESP_OK);
  assert(facade.task_.creates==0 && output.info.get_channels()==channels);
  assert(output.starts==1 && output.info.get_sample_rate()==48000);
  facade.audio_stream_info_=audio::AudioStreamInfo(16,channels,44100);
  assert(facade.requires_resampling_() && facade.start_()==ESP_OK);
  assert(facade.task_.creates==1 && output.info.get_channels()==channels);
  facade.audio_stream_info_=audio::AudioStreamInfo(32,channels,48000);
  assert(!facade.requires_resampling_() && facade.start_()==ESP_OK);
  assert(facade.task_.creates==1 && output.info.get_bits_per_sample()==32);
  facade.passthrough_bits_per_sample_=false;
  assert(facade.requires_resampling_() && facade.start_()==ESP_OK);
  assert(facade.task_.creates==2 && output.info.get_channels()==channels);
  assert(output.info.get_bits_per_sample()==16);
 }
}
''')
    library = Path(os.environ.get('AUDIO_LIBS_REPO', ROOT / '.test-deps/esp-audio-libs'))
    binary = folder / 'facade-bypass'
    built = subprocess.run([
        'g++', '-std=c++20', '-DUSE_ESP32', '-DUSE_SPEAKER', '-I', str(folder),
        '-I', str(ROOT / 'esphome/components/audio'), '-I', str(library / 'include'),
        str(cpp), str(ROOT / 'esphome/components/audio/audio.cpp'), '-o', str(binary),
    ], capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(('requested', 'enabled'), [(1, True), (2, False), (None, False)])
def test_native_final_validation_resolves_the_actual_mixer_parent(tmp_path, requested, enabled):
    """Exercise the actual ID resolver and final validators, without codegen/build."""
    companion = _companion_facade().parents[2]
    config = tmp_path / 'mono-policy.yaml'
    channels = '' if requested is None else f'    num_channels: {requested}\n'
    config.write_text(f'''esphome:
  name: pruning-policy
esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf
external_components:
  - source:
      type: local
      path: {ROOT / 'esphome/components'}
    components: [audio, mixer]
  - source:
      type: local
      path: {companion}
    components: [resampler]
i2s_audio:
  i2s_bclk_pin: GPIO6
  i2s_lrclk_pin: GPIO7
speaker:
  - platform: i2s_audio
    id: hw_speaker
    dac_type: external
    i2s_dout_pin: GPIO8
    channel: mono
    sample_rate: 48000
    bits_per_sample: 16bit
  - platform: mixer
    id: mono_renderer
    output_speaker: hw_speaker
    num_channels: 1
    source_speakers:
      - id: media_mixer
        sample_rate: 48000
        num_channels: 1
        bits_per_sample: 16
      - id: second_mixer
        sample_rate: 48000
        num_channels: 1
        bits_per_sample: 16
  - platform: resampler
    id: media_resampler
    output_speaker: media_mixer
{channels}    sample_rate: 48000
    bits_per_sample: 16
''')
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', str(config)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'_mono_mixer_output: {str(enabled).lower()}' in result.stdout
