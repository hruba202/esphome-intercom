"""Exercise upstream counters for continuous streams and real source lifecycle.

The production source setup/loop and mixer accounting statements are compiled.
FreeRTOS events, input buffers and the output sink are host adapters. The tests
cover contiguous contributions, including zero-valued PCM, rather than claiming
arbitrary intermittent contributions have exact attribution.
"""
import subprocess

import pytest

from tests.esphome_native import NATIVE_COMPONENTS


def method(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


@pytest.fixture(scope='module')
def mixer_binary(tmp_path_factory):
    source = (NATIVE_COMPONENTS / 'mixer/speaker/mixer_speaker.cpp').read_text()
    start = source.index('          // Set playback delay for newly contributing source\n')
    end = source.index('\n        } else {', start)
    single = source[start:end]
    start = source.index('        // Get current pipeline depth for delay calculation')
    end = source.index('\n      }\n    }', start)
    mixed = source[start:end]
    code = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <functional>
#include <memory>
#include <optional>
#include <vector>
using esp_err_t=int; constexpr int ESP_OK=0,ESP_ERR_NO_MEM=1,ESP_ERR_NOT_SUPPORTED=2,ESP_ERR_INVALID_ARG=3,ESP_ERR_INVALID_STATE=4;
namespace speaker {enum State {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
constexpr unsigned SOURCE_SPEAKER_COMMAND_START=1,SOURCE_SPEAKER_COMMAND_STOP=2,SOURCE_SPEAKER_COMMAND_FINISH=4,STOPPING_TIMEOUT_MS=5000;
unsigned clock_ms=1000;unsigned millis(){return clock_ms;}
unsigned xEventGroupGetBits(unsigned *p){return *p;}
void xEventGroupClearBits(unsigned *p,unsigned n){*p&=~n;}
struct Info {unsigned frames_to_bytes(unsigned n){return n*2;}};
struct Input {bool buffered=false;unsigned consumed=0;bool has_buffered_data(){return buffered;}void consume(unsigned n){consumed+=n;}};
struct Transfer {unsigned bytes=0;void increase_buffer_length(unsigned n){bytes+=n;}};
using Callback=std::function<void(uint32_t,int64_t)>;
struct Output {
 std::vector<Callback> callbacks;unsigned stops=0;bool stopped=false,paused=false;
 void add_audio_output_callback(Callback c){callbacks.push_back(std::move(c));}
 void emit(unsigned n){for(auto &c:callbacks)c(n,123456);}
 bool get_pause_state(){return paused;}bool is_stopped(){return stopped;}
 void stop(){++stops;stopped=true;}
};
struct MixerSpeaker {
 unsigned bits=0,*event_group_=&bits;Output output;Output *output_speaker_=&output;
 std::atomic<uint32_t> frames_in_pipeline_{0};
 Output *get_output_speaker(){return output_speaker_;}
 void disable_loop(){}void setup();
};
struct SourceSpeaker {
 MixerSpeaker *parent_;unsigned bits=0,*event_group_=&bits;
 speaker::State state_=speaker::STATE_STOPPED;
 std::atomic<bool> has_contributed_{false};
 std::atomic<uint32_t> pending_playback_frames_{0},playback_delay_frames_{0};
 std::shared_ptr<Input> audio_source_=std::make_shared<Input>();
 unsigned stopping_start_ms_=0,last_seen_data_ms_=0;
 std::optional<unsigned> timeout_ms_;bool stop_gracefully_=false,pause_state_=false;
 std::vector<unsigned> completed;
 Info get_audio_stream_info(){return {};}
 void audio_output_callback_(unsigned n,int64_t t){assert(t==123456);completed.push_back(n);}
 esp_err_t start_(){audio_source_=std::make_shared<Input>();return ESP_OK;}
 void disable_loop(){}void status_clear_error(){}void status_set_error(const char*){}
 void setup();void loop();void enter_stopping_state_();
};
#define LOG_STR(x) x
bool create_event_group(unsigned *&,void*){return true;}
'''
    code += method(source, 'static inline uint32_t atomic_subtract_clamped(') + '\n'
    for signature in ('void SourceSpeaker::setup()', 'void SourceSpeaker::loop()',
                      'void SourceSpeaker::enter_stopping_state_()', 'void MixerSpeaker::setup()'):
        code += method(source, signature) + '\n'
    code += r'''
struct Harness {
 MixerSpeaker mixer;SourceSpeaker a{&mixer},b{&mixer};std::deque<unsigned> wire;
 unsigned expected[2]{},observed[2]{};
 Harness(){mixer.setup();a.setup();b.setup();start(a);start(b);}
 void start(SourceSpeaker &s){s.state_=speaker::STATE_STARTING;s.loop();assert(s.state_==speaker::STATE_RUNNING);}
 void produce(unsigned mask,unsigned frames){
  auto *this_mixer=&mixer;unsigned frames_to_mix=frames;Info output_info;
  std::vector<SourceSpeaker*> speakers_with_data;
  std::vector<std::shared_ptr<Input>> audio_sources_with_data;
  if(mask&1){speakers_with_data.push_back(&a);audio_sources_with_data.push_back(a.audio_source_);}
  if(mask&2){speakers_with_data.push_back(&b);audio_sources_with_data.push_back(b.audio_source_);}
  Transfer transfer;auto *output_transfer_buffer=&transfer;
  Info active_stream_info;
  if(speakers_with_data.size()==1){
''' + single + r'''
  }else{
''' + mixed + r'''
  }
  assert(transfer.bytes==frames*2);
  for(unsigned n=0;n<frames;++n)wire.push_back(mask);
 }
 void complete(unsigned frames){
  assert(frames<=wire.size());
  for(unsigned n=0;n<frames;++n){auto mask=wire.front();wire.pop_front();for(unsigned i=0;i<2;++i)if(mask&(1U<<i))++expected[i];}
  mixer.output.emit(frames);
  SourceSpeaker *sources[2]={&a,&b};
  for(unsigned i=0;i<2;++i){observed[i]=0;for(unsigned n:sources[i]->completed)observed[i]+=n;assert(observed[i]==expected[i]);}
  assert(mixer.frames_in_pipeline_==wire.size());
 }
 void drain(){while(!wire.empty())complete(std::min<size_t>(7,wire.size()));}
};
int main(int argc,char **argv){
 assert(argc==2);unsigned scenario=std::atoi(argv[1]);Harness h;
 if(scenario==0){
  // Each active source contributes continuously; different start/end times overlap.
  h.produce(2,31);h.produce(3,53);h.produce(3,17);h.produce(1,29);h.drain();
  assert(h.observed[0]==99 && h.observed[1]==101);
 }else if(scenario==1){
  // Silence still consists of contributed frames, as in Sendspin's underrun path.
  for(unsigned i=0;i<20;++i){h.produce(1,16);h.complete(16);}
  assert(h.observed[0]==320 && h.a.pending_playback_frames_==0);
 }else if(scenario==2){
  h.produce(3,40);h.a.bits=SOURCE_SPEAKER_COMMAND_STOP;h.a.loop();
  assert(h.a.state_==speaker::STATE_STOPPING && h.mixer.output.stops==0);
  h.complete(40);h.a.loop();assert(h.a.state_==speaker::STATE_STOPPED);
  h.produce(2,20);h.complete(20);assert(h.b.state_==speaker::STATE_RUNNING);
  assert(h.mixer.output.stops==0);
 }else{
  h.produce(1,40);h.a.bits=SOURCE_SPEAKER_COMMAND_FINISH;h.a.loop();
  assert(h.a.state_==speaker::STATE_RUNNING);h.drain();h.a.loop();h.a.loop();
  assert(h.a.state_==speaker::STATE_STOPPED && h.mixer.output.stops==0);
  // A new source session resets its first-contribution delay with other audio queued.
  h.produce(2,23);h.start(h.a);h.produce(1,19);h.complete(23);
  assert(h.observed[0]==40);h.complete(19);assert(h.observed[0]==59);
 }
}
'''
    work = tmp_path_factory.mktemp('mixer-upstream-counter-contract')
    cpp, binary = work / 'counter.cpp', work / 'counter'
    cpp.write_text(code)
    subprocess.run(['g++', '-std=c++17', '-O1', '-fsanitize=address,undefined', str(cpp), '-o', str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize('scenario', range(4), ids=['overlap', 'continuous-silence', 'independent-stop', 'new-session'])
def test_standard_stream_accounting_and_source_lifecycle(mixer_binary, scenario):
    result = subprocess.run([str(mixer_binary), str(scenario)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
