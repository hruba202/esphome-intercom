"""Compose production player accounting, resampler writes and RingBuffer I/O.

The RTOS byte-ring scheduler is a deterministic test substitute. PCM passes
through the current ESPHome RingBuffer and our current resampler writer; no
frozen audit implementation is imported.
"""

from pathlib import Path
import os
import resource
import subprocess

import esphome
import pytest


ROOT = Path(__file__).resolve().parents[1]
PLAYER = Path(os.environ.get("SPEAKER_SOURCE_COMPONENT", ROOT / "esphome/components/speaker_source"))
RESAMPLER = ROOT.parent / "esphome-audio-stack/esphome/components/resampler/speaker"
UPSTREAM = Path(esphome.__file__).parent


def block(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start : index + 1]
    raise AssertionError(signature)


@pytest.fixture(scope="module")
def accounting_binary(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("player-byte-accounting")
    headers = {
        "esphome/core/defines.h": "#pragma once\n",
        "esphome/core/helpers.h": r'''#pragma once
#include <algorithm>
#include <cstring>
#include <memory>
#include <cstdlib>
#define ESPDEPRECATED(...)
namespace esphome {
using std::make_unique;
template<class T>struct RAMAllocator {
 static constexpr unsigned PREFER_INTERNAL=1,NONE=0;
 explicit RAMAllocator(unsigned=0){}
 T *allocate(size_t n){return static_cast<T*>(std::malloc(n*sizeof(T)));}
 void deallocate(T *p,size_t){std::free(p);}
};}
''',
        "esphome/core/log.h": "#pragma once\n#define ESP_LOGD(...)\n",
        "freertos/FreeRTOS.h": r'''#pragma once
#include <cstdint>
using TickType_t=unsigned;using UBaseType_t=unsigned;using BaseType_t=int;
constexpr int pdPASS=1;
''',
        "freertos/ringbuf.h": r'''#pragma once
#include "FreeRTOS.h"
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <vector>
inline std::function<void()> wait_hook;
enum RingbufferType_t{RINGBUF_TYPE_BYTEBUF};
struct StaticRingbuffer_t{size_t capacity=0,leased=0;std::vector<uint8_t> data;};
using RingbufHandle_t=StaticRingbuffer_t*;
inline RingbufHandle_t xRingbufferCreateStatic(size_t n,RingbufferType_t,uint8_t*,StaticRingbuffer_t*s){s->capacity=n;return s;}
inline void vRingbufferDelete(RingbufHandle_t){}
inline bool xRingbufferSend(RingbufHandle_t s,const void *p,size_t n,TickType_t ticks){
 if(n>s->capacity-s->data.size() && ticks && wait_hook){auto f=std::move(wait_hook);wait_hook={};f();}
 if(n>s->capacity-s->data.size())return false;
 auto *bytes=static_cast<const uint8_t*>(p);s->data.insert(s->data.end(),bytes,bytes+n);return true;
}
inline size_t xRingbufferGetCurFreeSize(RingbufHandle_t s){return s->capacity-s->data.size();}
inline void vRingbufferGetInfo(RingbufHandle_t s,void*,void*,void*,void*,unsigned*n){*n=s->data.size();}
inline void *xRingbufferReceiveUpTo(RingbufHandle_t s,size_t*n,TickType_t,size_t max){
 *n=std::min(max,s->data.size());s->leased=*n;return *n?s->data.data():nullptr;
}
inline void vRingbufferReturnItem(RingbufHandle_t s,void*){s->data.erase(s->data.begin(),s->data.begin()+s->leased);s->leased=0;}
''',
    }
    for name, text in headers.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    text = (PLAYER / "speaker_source_media_player.cpp").read_text()
    context = block((PLAYER / "speaker_source_media_player.h").read_text(), "struct PipelineContext") + ";"
    guard = block(text, "class PlaybackOperationGuard") + ";" if "class PlaybackOperationGuard" in text else ""
    methods = "\n".join(block(text, signature) for signature in (
        "void SpeakerSourceMediaPlayer::handle_speaker_playback_callback_",
        "size_t SpeakerSourceMediaPlayer::handle_media_output_",
        "void SpeakerSourceMediaPlayer::handle_media_state_changed_",
        "void SpeakerSourceMediaPlayer::handle_player_command_",
        "bool SpeakerSourceMediaPlayer::try_execute_play_uri_",
    ))
    writer = block((RESAMPLER / "resampler_speaker.cpp").read_text(), "size_t ResamplerSpeaker::play")
    writer = writer.replace("ResamplerSpeaker::play(", "ResamplerSpeaker::play_actual(", 1)
    http_text = (UPSTREAM / "components/audio_http/audio_http_media_source.cpp").read_text()
    http_methods = "\n".join(block(http_text, signature) for signature in (
        "size_t AudioHTTPMediaSource::on_audio_write", "void AudioHTTPMediaSource::on_state_change",
    ))
    code = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>
#include <cstdio>
#define ESP_LOGV(...)
#define ESP_LOGW(...)
#define ESP_LOGE(...)
#include "esphome/components/audio/audio.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
using namespace esphome;
std::function<void()> delay_hook;
unsigned waited=0;void vTaskDelay(unsigned ticks){waited+=ticks;auto hook=delay_hook;if(hook)hook();}
unsigned pdMS_TO_TICKS(unsigned n){return n;}
struct Component {unsigned wakes=0;void enable_loop_soon_any_context(){++wakes;}};
struct FakeSpeaker {
 std::function<void()> during_write;
 size_t accept=SIZE_MAX;
 bool is_running()const{return true;}
 size_t play(const uint8_t*,size_t n,unsigned){if(during_write)during_write();return std::min(accept,n);}
};
struct ResamplerSpeaker {
 std::weak_ptr<ring_buffer::RingBuffer> ring_buffer_;
 FakeSpeaker output;FakeSpeaker *output_speaker_=&output;
 audio::AudioStreamInfo info;
 unsigned stops=0,starts=0;
 std::function<void()> before_play;
 bool stopped=false;bool is_stopped()const{return stopped;}void start(){++starts;stopped=false;}
 void stop(){++stops;stopped=true;}void finish(){}
 bool bypass=false;bool requires_resampling_()const{return !bypass;}
 auto get_audio_stream_info()const{return info;}
 void set_audio_stream_info(const audio::AudioStreamInfo &i){info=i;}
 size_t play_actual(const uint8_t*,size_t,TickType_t);
 size_t play(const uint8_t *p,size_t n,TickType_t ticks){auto hook=before_play;if(hook)hook();return play_actual(p,n,ticks);}
};
WRITER
namespace media_source {
enum class MediaSourceState {IDLE,PLAYING,PAUSED,ERROR};
enum class MediaSourceCommand {STOP,PLAY,PAUSE,NEXT,PREVIOUS,REPEAT_ONE,REPEAT_OFF,REPEAT_ALL,CLEAR_PLAYLIST,SHUFFLE,UNSHUFFLE};
struct MediaSource {
 unsigned completed=0,starts=0;MediaSourceState state=MediaSourceState::PLAYING;
 std::function<void()> on_start,on_stop,on_notify;
 void notify_audio_played(uint32_t n,int64_t){completed+=n;if(on_notify)on_notify();}
 auto get_state()const{return state;}bool has_internal_playlist()const{return false;}
 bool play_uri(const std::string&){++starts;if(on_start)on_start();return true;}
 void handle_command(MediaSourceCommand c){if(c==MediaSourceCommand::STOP){state=MediaSourceState::IDLE;if(on_stop)on_stop();}}
};}
namespace media_player {using MediaPlayerSupportedFormat=int;
enum MediaPlayerCommand {MEDIA_PLAYER_COMMAND_STOP,MEDIA_PLAYER_COMMAND_ENQUEUE,MEDIA_PLAYER_COMMAND_MUTE,
 MEDIA_PLAYER_COMMAND_UNMUTE,MEDIA_PLAYER_COMMAND_VOLUME_UP,MEDIA_PLAYER_COMMAND_VOLUME_DOWN,
 MEDIA_PLAYER_COMMAND_TOGGLE,MEDIA_PLAYER_COMMAND_PLAY,MEDIA_PLAYER_COMMAND_PAUSE,
 MEDIA_PLAYER_COMMAND_NEXT,MEDIA_PLAYER_COMMAND_PREVIOUS,MEDIA_PLAYER_COMMAND_REPEAT_ONE,
 MEDIA_PLAYER_COMMAND_REPEAT_OFF,MEDIA_PLAYER_COMMAND_REPEAT_ALL,MEDIA_PLAYER_COMMAND_CLEAR_PLAYLIST,
 MEDIA_PLAYER_COMMAND_SHUFFLE,MEDIA_PLAYER_COMMAND_UNSHUFFLE};
}
namespace speaker {using Speaker=ResamplerSpeaker;}
template<class T>using optional=std::optional<T>;
struct SourceBinding {};
enum RepeatMode:uint8_t{REPEAT_OFF,REPEAT_ONE,REPEAT_ALL};
const unsigned PIPELINE_TIMEOUT_IDS[]={1,2};
struct MediaPlayerControlCommand{enum Type{PLAY_CURRENT,PLAYLIST_ADVANCE};};
CONTEXT
GUARD
struct SpeakerSourceMediaPlayer:Component {
 PipelineContext pipelines_[2];media_source::MediaSource *requested_source=nullptr;
 unsigned advances=0;
 void cancel_timeout(unsigned){}void shuffle_playlist_(uint8_t){}void unshuffle_playlist_(uint8_t){}
 size_t get_playlist_position_(uint8_t p){return pipelines_[p].playlist_index;}
 void queue_command_(MediaPlayerControlCommand::Type,uint8_t){++advances;}
 media_source::MediaSource *find_source_for_uri_(const std::string&,uint8_t){return requested_source;}
 void handle_speaker_playback_callback_(uint32_t,int64_t,uint8_t);
 size_t handle_media_output_(uint8_t,media_source::MediaSource*,const uint8_t*,size_t,uint32_t,const audio::AudioStreamInfo&);
 void handle_media_state_changed_(uint8_t,media_source::MediaSource*,media_source::MediaSourceState);
 void handle_player_command_(media_player::MediaPlayerCommand,uint8_t);
 bool try_execute_play_uri_(const std::string&,uint8_t);
};
METHODS
namespace micro_decoder {enum class DecoderState{IDLE,PLAYING,FAILED};}
struct AudioHTTPMediaSource:media_source::MediaSource {
 std::atomic<bool> pause_{false};audio::AudioStreamInfo stream_info_;
 SpeakerSourceMediaPlayer *player=nullptr;
 void disable_loop(){}
 void set_state_(media_source::MediaSourceState s){state=s;player->handle_media_state_changed_(0,this,s);}
 size_t write_output(const uint8_t *p,size_t n,unsigned t,const audio::AudioStreamInfo &info){return player->handle_media_output_(0,this,p,n,t,info);}
 size_t on_audio_write(const uint8_t*,size_t,uint32_t);
 void on_state_change(micro_decoder::DecoderState);
};
constexpr unsigned PAUSE_POLL_DELAY_MS=20;
HTTP_METHODS
int exercise_session(unsigned scenario){
 SpeakerSourceMediaPlayer player;media_source::MediaSource old,next,announcement;
 auto &pipeline=player.pipelines_[0];
 const audio::AudioStreamInfo mono(16,1,48000),stereo(16,2,44100);
 ResamplerSpeaker speaker;speaker.info=mono;pipeline.speaker=&speaker;pipeline.active_source=&old;
 uint8_t pcm[64]{};
 next.state=media_source::MediaSourceState::IDLE;player.requested_source=&next;
 old.on_stop=[&]{player.handle_media_state_changed_(0,&old,media_source::MediaSourceState::IDLE);speaker.stop();};
 if(scenario==0){
  // A byte accepted in a retired session must not become half a new frame.
  auto rb=ring_buffer::RingBuffer::create(16);std::shared_ptr<ring_buffer::RingBuffer> ring(std::move(rb));speaker.ring_buffer_=ring;
  assert(player.handle_media_output_(0,&old,pcm,1,7,mono)==1);
  player.handle_player_command_(media_player::MEDIA_PLAYER_COMMAND_STOP,0);
  next.on_start=[&]{next.state=media_source::MediaSourceState::PLAYING;player.handle_media_state_changed_(0,&next,next.state);};
  assert(player.try_execute_play_uri_("test://next",0));ring->reset();
  speaker.info=stereo;
  assert(player.handle_media_output_(0,&next,pcm,4,7,stereo)==4);
  player.handle_speaker_playback_callback_(1,10,0);
  assert(next.completed==1 && old.completed==0 && pipeline.pending_bytes==0 && pipeline.frame_bytes==4);
 }else if(scenario==1){
  speaker.bypass=true;
  for(unsigned accepted:{0U,60U,100U}){
   old.completed=0;speaker.output.accept=accepted*2;
   speaker.output.during_write=[&]{player.handle_speaker_playback_callback_(accepted/2,10,0);};
   uint8_t data[200]{};
   assert(player.handle_media_output_(0,&old,data,sizeof(data),7,mono)==accepted*2);
   player.handle_speaker_playback_callback_(accepted-accepted/2,20,0);
   assert(old.completed==accepted && pipeline.pending_bytes==0);
  }
 }else if(scenario==2){
  auto rb=ring_buffer::RingBuffer::create(10);std::shared_ptr<ring_buffer::RingBuffer> ring(std::move(rb));speaker.ring_buffer_=ring;
  assert(player.handle_media_output_(0,&old,pcm,10,7,mono)==10);
  bool blocked=false;
  wait_hook=[&]{
   player.handle_player_command_(media_player::MEDIA_PLAYER_COMMAND_STOP,0);
   blocked=!player.try_execute_play_uri_("test://next",0);
   uint8_t discard[10];assert(ring->read(discard,10)==10);
  };
  assert(player.handle_media_output_(0,&old,pcm,10,7,mono)==1);
  assert(blocked && next.starts==0 && pipeline.playback_operations==0 && player.wakes>0);
  next.on_start=[&]{ring->reset();next.state=media_source::MediaSourceState::PLAYING;player.handle_media_state_changed_(0,&next,next.state);};
  assert(player.try_execute_play_uri_("test://next",0));
  assert(player.handle_media_output_(0,&next,pcm,2,7,mono)==2);
  player.handle_speaker_playback_callback_(1,20,0);
  assert(next.completed==1 && old.completed==0 && pipeline.pending_bytes==0);
 }else if(scenario==6){
  speaker.bypass=true;ResamplerSpeaker other;other.bypass=true;other.info=stereo;
  player.pipelines_[1].speaker=&other;player.pipelines_[1].active_source=&announcement;
  speaker.output.during_write=[&]{
   assert(player.handle_media_output_(1,&announcement,pcm,4,7,stereo)==4);
   player.handle_speaker_playback_callback_(1,10,1);
  };
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==2);
  player.handle_speaker_playback_callback_(1,20,0);
  assert(old.completed==1 && announcement.completed==1);
  assert(pipeline.pending_bytes==0 && player.pipelines_[1].pending_bytes==0);
 }else if(scenario==7){
  speaker.bypass=true;
  assert(player.handle_media_output_(0,&old,pcm,4,7,stereo)==0 && waited==7);
  assert(player.handle_media_output_(0,&old,pcm,4,7,stereo)==4);
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==0 && waited==14);
  player.handle_speaker_playback_callback_(1,20,0);
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==0 && waited==21);
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==2);
  player.handle_speaker_playback_callback_(1,30,0);
  assert(old.completed==2 && pipeline.pending_bytes==0);
 }else if(scenario==8){
  AudioHTTPMediaSource http;http.player=&player;http.stream_info_=mono;
  http.state=media_source::MediaSourceState::IDLE;
  pipeline.active_source=nullptr;pipeline.pending_source=&http;pipeline.invalidate_playback();
  speaker.bypass=true;
  // The decoder writes from its task before DecoderSource::loop delivers its
  // queued PLAYING event on the main loop. Deliver it during write backpressure.
  delay_hook=[&]{
   http.on_state_change(micro_decoder::DecoderState::PLAYING);
   assert(pipeline.active_source==&http && "HTTP PLAYING must be admitted while an unadmitted writer waits");
  };
  assert(http.on_audio_write(pcm,2,50)==0);
  delay_hook={};
  assert(waited==50 && pipeline.playback_operations==0 && pipeline.pending_source==nullptr);
  assert(http.on_audio_write(pcm,2,50)==2);
  player.handle_speaker_playback_callback_(1,30,0);
  assert(http.completed==1 && pipeline.pending_bytes==0);
 }else if(scenario==3){
  speaker.bypass=true;assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==2);
  bool blocked=false;
  old.on_notify=[&]{player.handle_player_command_(media_player::MEDIA_PLAYER_COMMAND_STOP,0);blocked=!player.try_execute_play_uri_("test://next",0);};
  player.handle_speaker_playback_callback_(1,20,0);
  assert(blocked && next.starts==0 && old.completed==1);
  // Synchronous PLAYING plus audio inside play_uri must keep its new credit.
  next.on_start=[&]{next.state=media_source::MediaSourceState::PLAYING;player.handle_media_state_changed_(0,&next,next.state);
   assert(player.handle_media_output_(0,&next,pcm,4,7,mono)==4);
  };
  assert(player.try_execute_play_uri_("test://next",0));
  player.handle_speaker_playback_callback_(2,30,0);
  assert(next.completed==2 && pipeline.pending_bytes==0);
 }else if(scenario==4){
  speaker.bypass=true;
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==2);
  player.handle_speaker_playback_callback_(1,10,0);
  speaker.info=stereo;
  assert(player.handle_media_output_(0,&old,pcm,4,7,stereo)==4);
  player.handle_speaker_playback_callback_(1,20,0);
  assert(old.completed==2 && pipeline.pending_bytes==0 && pipeline.frame_bytes==4);
 }else if(scenario==5){
  speaker.bypass=true;
  bool blocked=false;
  speaker.before_play=[&]{
   speaker.before_play={};
   player.handle_player_command_(media_player::MEDIA_PLAYER_COMMAND_STOP,0);
   blocked=!player.try_execute_play_uri_("test://next",0);
  };
  assert(player.handle_media_output_(0,&old,pcm,2,7,mono)==2);
  assert(blocked && speaker.starts==1 && speaker.stops==1);
  next.on_start=[&]{next.state=media_source::MediaSourceState::PLAYING;player.handle_media_state_changed_(0,&next,next.state);};
  assert(player.try_execute_play_uri_("test://next",0) && "retired write must complete STOP before replacement starts");
  assert(speaker.stops==2 && next.starts==1 && pipeline.active_source==&next);
  assert(player.handle_media_output_(0,&next,pcm,2,7,mono)==2);
  player.handle_speaker_playback_callback_(1,20,0);
  assert(next.completed==1 && old.completed==0 && pipeline.pending_bytes==0);
 }
 assert(pipeline.playback_operations==0);
 return 0;
}
int main(int argc,char **argv){
 assert(argc==2 || argc==3);if(argc==3)return exercise_session(std::stoul(argv[2]));
 const unsigned channels=std::stoul(argv[1]);
 const unsigned frame=channels*2,capacity=frame*5,cycles=1000;
 auto owned=ring_buffer::RingBuffer::create(capacity);
 std::shared_ptr<ring_buffer::RingBuffer> ring(std::move(owned));
 ResamplerSpeaker speaker;speaker.ring_buffer_=ring;
 const audio::AudioStreamInfo info(16,channels,44100);speaker.info=info;
 SpeakerSourceMediaPlayer player;media_source::MediaSource source;
 player.pipelines_[0].speaker=&speaker;player.pipelines_[0].active_source=&source;
 std::vector<uint8_t> sent(capacity*(cycles+1)),heard;
 for(unsigned i=0;i<sent.size();i++)sent[i]=(i*37+19)%251;
 auto drain=[&]{std::vector<uint8_t> out(capacity);auto n=ring->read(out.data(),capacity);assert(n==capacity);heard.insert(heard.end(),out.begin(),out.end());};
 auto send=[&](size_t offset,size_t n){return player.handle_media_output_(0,&source,sent.data()+offset,n,7,info);};
 assert(send(0,capacity)==capacity);
 for(unsigned cycle=0;cycle<cycles;cycle++){
  wait_hook=drain;const size_t offset=(cycle+1)*capacity;
  const size_t first=send(offset,capacity);assert(first==1);
  player.handle_speaker_playback_callback_(5,cycle,0);
  assert(send(offset+first,capacity-first)==capacity-first);
 }
 drain();player.handle_speaker_playback_callback_(5,99999,0);
 assert(heard==sent && "partial resampler writes must preserve all PCM bytes");
 assert(source.completed==sent.size()/frame && "byte-fragment accounting must preserve every completed frame");
 std::printf("channels=%u frames=%zu notified=%u pcm_bytes=%zu cycles=%u\n",channels,sent.size()/frame,source.completed,heard.size(),cycles);
}
'''.replace("CONTEXT", context).replace("GUARD", guard).replace("WRITER", writer).replace("HTTP_METHODS", http_methods).replace("METHODS", methods)
    cpp, binary = tmp_path / "case.cpp", tmp_path / "case"
    cpp.write_text(code)
    audio_source = (UPSTREAM / "components/audio/audio.cpp").read_text()
    audio_methods = "\n".join(block(audio_source, signature) for signature in (
        "static uint32_t gcd", "AudioStreamInfo::AudioStreamInfo(uint8_t", "bool AudioStreamInfo::operator==",
    ))
    adapter = tmp_path / "audio_format.cpp"
    adapter.write_text('#include "esphome/components/audio/audio.h"\nnamespace esphome::audio {\n' + audio_methods + "\n}\n")
    subprocess.run([
        "g++", "-std=c++20", "-DUSE_ESP32", "-fsanitize=address,undefined", "-I", str(tmp_path),
        "-I", str(UPSTREAM.parent), str(cpp), str(adapter),
        str(UPSTREAM / "components/ring_buffer/ring_buffer.cpp"), "-o", str(binary),
    ], check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("channels", [1, 2], ids=["mono", "stereo"])
def test_resampler_byte_fragments_keep_player_completion(accounting_binary, channels):
    result = subprocess.run(
        [str(accounting_binary), str(channels)], capture_output=True, text=True,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", [0, 1, 2, 3, 4, 5, 6, 7, 8], ids=[
    "retired-partial-frame", "callback-during-bypass", "old-write-after-stop",
    "old-notify-and-synchronous-new-start", "drained-format-change",
    "stop-in-gap-before-speaker-play",
    "independent-pipelines", "format-rejection-backpressure",
    "deferred-http-playing-during-writer-retry",
])
def test_playback_accounting_is_session_and_format_scoped(accounting_binary, scenario):
    result = subprocess.run(
        [str(accounting_binary), "1", str(scenario)], capture_output=True, text=True,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
