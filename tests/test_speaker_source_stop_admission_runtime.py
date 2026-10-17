"""STOP must reach its source before a blocking OTA phase and supersede queued work."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def test_stop_preempts_only_its_pipeline_and_releases_pending_reader(tmp_path):
    text = (ROOT / 'esphome/components/speaker_source/speaker_source_media_player.cpp').read_text()
    code = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <deque>
#include <optional>
#include <string>
#include <vector>
#include <cstdint>
void* watched[4]{};unsigned freed[4]{};
void* operator new(std::size_t n){if(auto p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p) noexcept {for(unsigned i=0;i<4;++i)if(p&&p==watched[i])++freed[i];std::free(p);}
void operator delete(void* p,std::size_t) noexcept {::operator delete(p);}
#define ESP_LOGE(...) ((void)0)
constexpr int pdTRUE=1;
constexpr unsigned MEDIA_PIPELINE=0,ANNOUNCEMENT_PIPELINE=1;
const char* PIPELINE_TIMEOUT_IDS[]={"media","announcement"};
constexpr unsigned REPEAT_OFF=0,REPEAT_ONE=1,REPEAT_ALL=2;
namespace media_player {
enum MediaPlayerCommand {MEDIA_PLAYER_COMMAND_STOP,MEDIA_PLAYER_COMMAND_ENQUEUE,MEDIA_PLAYER_COMMAND_MUTE,
 MEDIA_PLAYER_COMMAND_UNMUTE,MEDIA_PLAYER_COMMAND_VOLUME_UP,MEDIA_PLAYER_COMMAND_VOLUME_DOWN,
 MEDIA_PLAYER_COMMAND_TOGGLE,MEDIA_PLAYER_COMMAND_PLAY,MEDIA_PLAYER_COMMAND_PAUSE,
 MEDIA_PLAYER_COMMAND_NEXT,MEDIA_PLAYER_COMMAND_PREVIOUS,MEDIA_PLAYER_COMMAND_REPEAT_ONE,
 MEDIA_PLAYER_COMMAND_REPEAT_OFF,MEDIA_PLAYER_COMMAND_REPEAT_ALL,MEDIA_PLAYER_COMMAND_CLEAR_PLAYLIST,
 MEDIA_PLAYER_COMMAND_SHUFFLE,MEDIA_PLAYER_COMMAND_UNSHUFFLE};
struct MediaPlayerCall {
 std::optional<bool> announcement;std::optional<std::string> url;std::optional<MediaPlayerCommand> command;
 auto get_announcement()const{return announcement;}const auto& get_media_url()const{return url;}
 auto get_command()const{return command;}std::optional<float> get_volume()const{return {};}
};
}
namespace media_source {
enum class MediaSourceCommand {STOP,PLAY,PAUSE,NEXT,PREVIOUS,REPEAT_ONE,REPEAT_OFF,REPEAT_ALL,CLEAR_PLAYLIST,SHUFFLE,UNSHUFFLE};
enum class MediaSourceState {IDLE,PLAYING};
struct MediaSource {
 bool opening=true,internal=false;unsigned stops=0;
 MediaSourceState state=MediaSourceState::PLAYING;
 bool has_internal_playlist(){return internal;}auto get_state(){return state;}
 void handle_command(MediaSourceCommand c){if(c==MediaSourceCommand::STOP){++stops;opening=false;state=MediaSourceState::IDLE;}}
};
}
struct MediaPlayerControlCommand {
 enum Type {PLAY_URI,ENQUEUE_URI,PLAYLIST_ADVANCE,PLAY_CURRENT,SEND_COMMAND} type=PLAY_CURRENT;
 uint8_t pipeline=0;
 union Data {std::string* uri;media_player::MediaPlayerCommand command;Data():uri(nullptr){}} data;
};
using Queue=std::deque<MediaPlayerControlCommand>;
int xQueueSend(Queue* q,const MediaPlayerControlCommand* c,int){if(q->size()==20)return 0;q->push_back(*c);return 1;}
int xQueueReceive(Queue* q,MediaPlayerControlCommand* c,int){if(q->empty())return 0;*c=q->front();q->pop_front();return 1;}
unsigned uxQueueMessagesWaiting(Queue* q){return q->size();}
struct PipelineContext {
 std::atomic<media_source::MediaSource*> active_source{nullptr};
 media_source::MediaSource *pending_source=nullptr,*last_source=nullptr,*stopping_source=nullptr;
 std::vector<std::string> playlist;std::vector<unsigned> shuffle_indices;
 unsigned playlist_index=0,repeat_mode=0;bool is_configured(){return true;}
};
struct SpeakerSourceMediaPlayer {
 Queue queue;Queue* media_control_command_queue_=&queue;std::array<PipelineContext,2> pipelines_;
 float volume=.01f,volume_increment_=.1f;
 bool is_ready(){return true;}void set_volume_(float){}void set_mute_state_(bool){}
 void publish_state(){}void dump_diagnostics(){}void cancel_timeout(const char*){}
 unsigned get_playlist_position_(uint8_t p){return pipelines_[p].playlist_index;}
 void shuffle_playlist_(uint8_t){}void unshuffle_playlist_(uint8_t){}
 void queue_command_(MediaPlayerControlCommand::Type t,uint8_t p){MediaPlayerControlCommand c;c.type=t;c.pipeline=p;assert(xQueueSend(&queue,&c,0));}
 void discard_pipeline_commands_(uint8_t);
 void handle_player_command_(media_player::MediaPlayerCommand,uint8_t);
 void control(const media_player::MediaPlayerCall&);
};
'''
    code += method(text, 'void SpeakerSourceMediaPlayer::control(')
    code += method(text, 'void SpeakerSourceMediaPlayer::handle_player_command_(')
    signature = 'void SpeakerSourceMediaPlayer::discard_pipeline_commands_('
    if signature in text:
        code += method(text, signature)
    code += r'''
int main(){
 for(bool pending:{false,true}) {
  SpeakerSourceMediaPlayer p;media_source::MediaSource a,b;
  p.pipelines_[1].active_source=&b;
  if(pending){a.state=media_source::MediaSourceState::IDLE;p.pipelines_[0].pending_source=&a;}
  else p.pipelines_[0].active_source=&a;
  // Includes a blocked play-current head and independently queued announcements.
  p.queue_command_(MediaPlayerControlCommand::PLAY_CURRENT,0);
  MediaPlayerControlCommand own,other;own.type=other.type=MediaPlayerControlCommand::PLAY_URI;
  own.pipeline=0;other.pipeline=1;own.data.uri=new std::string("cancelled");other.data.uri=new std::string("announcement");
  watched[0]=own.data.uri;watched[1]=other.data.uri;freed[0]=freed[1]=0;
  xQueueSend(&p.queue,&own,0);xQueueSend(&p.queue,&other,0);
  p.queue_command_(MediaPlayerControlCommand::PLAY_CURRENT,1);
  media_player::MediaPlayerCall stop;stop.command=media_player::MEDIA_PLAYER_COMMAND_STOP;
  p.control(stop);
  // No main-loop tick is allowed between this command and OTA begin.
  assert(a.stops==1 && !a.opening && b.stops==0);
  assert(p.queue.size()==2 && p.queue[0].pipeline==1 && p.queue[1].pipeline==1);
  assert(p.queue[0].data.uri==other.data.uri && freed[0]==1 && freed[1]==0);
  media_player::MediaPlayerCall next;next.url="next";p.control(next);
  assert(p.queue.size()==3 && p.queue.back().pipeline==0);
  p.control(stop);assert(p.queue.size()==2 && b.stops==0);
  stop.announcement=true;p.control(stop);assert(p.queue.empty() && b.stops==1 && freed[1]==1);
  watched[0]=watched[1]=nullptr;
 }
 // A full queue belonging to the other pipeline remains full and in order.
 SpeakerSourceMediaPlayer full;
 for(unsigned i=0;i<20;++i)full.queue_command_(MediaPlayerControlCommand::PLAY_CURRENT,1);
 media_player::MediaPlayerCall stop;stop.command=media_player::MEDIA_PLAYER_COMMAND_STOP;
 full.control(stop);assert(full.queue.size()==20);
 for(const auto &item:full.queue)assert(item.pipeline==1 && item.type==MediaPlayerControlCommand::PLAY_CURRENT);
 stop.announcement=true;full.control(stop);assert(full.queue.empty());
}
'''
    source = tmp_path / 'admission.cpp'
    source.write_text(code)
    binary = tmp_path / 'admission'
    subprocess.run(['g++', '-std=c++17', str(source), '-o', str(binary)], check=True, capture_output=True, text=True)
    result = subprocess.run(['bash', '-c', 'ulimit -c 0; exec "$1"', 'bash', str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
