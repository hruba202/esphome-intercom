"""Exercise the shipped TTS terminal-event handler with reordered completion."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_tts_end_after_playback_completion(tmp_path):
    source = (ROOT / 'esphome/components/voice_assistant/voice_assistant.cpp').read_text()
    case = source.split('case api::enums::VOICE_ASSISTANT_TTS_END: {', 1)[1].split('case api::enums::VOICE_ASSISTANT_RUN_END:', 1)[0]
    event_handler = source.split('void VoiceAssistant::on_event(', 1)[1]
    event_guard = event_handler.split('msg.event_type);', 1)[1].split('switch (msg.event_type)', 1)[0]
    stub = r'''
#define USE_MEDIA_PLAYER
#define ESP_LOGW(...)
#define ESP_LOGD(...)
#include <string>
#include <vector>
#include <functional>
#include <cassert>
enum class MediaPlayerResponseState {IDLE, URL_SENT, PLAYING, FINISHED, ABORTED};
enum class State {IDLE, STREAMING_RESPONSE};
struct Arg {std::string name, value;};
namespace api::enums {enum {VOICE_ASSISTANT_TTS_START, VOICE_ASSISTANT_TTS_END,
 VOICE_ASSISTANT_INTENT_PROGRESS, VOICE_ASSISTANT_INTENT_END};}
struct Msg {std::vector<Arg> data; int event_type=api::enums::VOICE_ASSISTANT_TTS_END;};
struct Player {
 int requests=0;
 Player& make_call(){return *this;}
 Player& set_media_url(const std::string&){return *this;}
 Player& set_announcement(bool){return *this;}
 void perform(){++requests;}
};
struct Trigger {int calls=0; void trigger(const std::string&){++calls;}};
struct Voice {
 Player player; Player* media_player_=&player;
 MediaPlayerResponseState media_player_response_state_=MediaPlayerResponseState::IDLE;
 State state_=State::IDLE; bool local_output_=true, started_streaming_tts_=false;
 Trigger tts_end_trigger_; int timers=0; std::vector<std::function<void()>> jobs;
 void defer(std::function<void()> f){jobs.push_back(f);}
 void start_playback_timeout_(){++timers;}
 void set_state_(State s,State){state_=s;}
 void flush(){for(auto& f:jobs)f();jobs.clear();}
 void end(const Msg& msg){switch(0){case 0:{
'''
    checks = r'''
}}
};
int main(){
 Msg end{{{"url","http://test/response"}}};
 for(bool streamed:{false,true}){
  Voice v;v.started_streaming_tts_=streamed;
  v.media_player_response_state_=MediaPlayerResponseState::FINISHED;
  v.end(end);v.flush();
  assert(v.state_==State::IDLE);assert(v.player.requests==0);assert(v.timers==0);
 }
 Voice aborted;aborted.media_player_response_state_=MediaPlayerResponseState::ABORTED;
 aborted.end(end);aborted.flush();assert(aborted.player.requests==0);assert(aborted.state_==State::IDLE);
 Voice normal;normal.end(end);normal.flush();assert(normal.player.requests==1);assert(normal.timers==1);
 assert(normal.state_==State::STREAMING_RESPONSE);
 Voice streaming;streaming.started_streaming_tts_=true;
 streaming.media_player_response_state_=MediaPlayerResponseState::PLAYING;
 streaming.end(end);streaming.flush();assert(streaming.player.requests==0);
}
'''
    cpp = tmp_path / 'response.cpp'
    stub = stub.replace('void end(const Msg& msg){switch', 'void end(const Msg& msg){' + event_guard + 'switch')
    cpp.write_text(stub + case + checks)
    binary = tmp_path / 'response'
    subprocess.run(['g++', '-std=c++20', str(cpp), '-o', str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)


def test_stopping_response_completes_announcement_once(tmp_path):
    source = (ROOT / "esphome/components/voice_assistant/voice_assistant.cpp").read_text()
    body = source.split("void VoiceAssistant::request_stop() {", 1)[1].split(
        "void VoiceAssistant::signal_stop_()", 1
    )[0]
    stub = r'''
#define USE_MEDIA_PLAYER
#include <cassert>
#include <vector>
enum class MediaPlayerResponseState {IDLE, URL_SENT, PLAYING, FINISHED, ABORTED};
enum class State {IDLE, START_MICROPHONE, STARTING_MICROPHONE, WAIT_FOR_VAD,
 WAITING_FOR_VAD, START_PIPELINE, STARTING_PIPELINE, STREAMING_MICROPHONE,
 STOP_MICROPHONE, STOPPING_MICROPHONE, AWAITING_RESPONSE, STREAMING_RESPONSE,
 RESPONSE_FINISHED};
namespace media_player {constexpr int MEDIA_PLAYER_COMMAND_STOP=0;}
struct Player {
 int stop_requests=0;
 Player& make_call(){return *this;}
 Player& set_command(int){return *this;}
 Player& set_announcement(bool){return *this;}
 void perform(){++stop_requests;}
};
struct Voice {
 bool continuous_=false, continue_conversation_=false, started_streaming_tts_=false;
 Player player; Player *media_player_=&player;
 MediaPlayerResponseState media_player_response_state_=MediaPlayerResponseState::IDLE;
 State state_=State::STREAMING_RESPONSE, desired_state_=State::IDLE;
 std::vector<bool> completions;
 void cancel_timeout(const char*){}
 void set_state_(State state,State desired){state_=state;desired_state_=desired;}
 void signal_stop_(){}
 void send_announce_finished_(bool success){completions.push_back(success);}
 void stop(){
'''
    checks = r'''
};
int main(){
 for(auto state:{MediaPlayerResponseState::URL_SENT,MediaPlayerResponseState::PLAYING}){
  Voice v;v.media_player_response_state_=state;v.stop();v.stop();
  assert(v.completions.size()==1 && !v.completions.front());
  assert(v.media_player_response_state_==MediaPlayerResponseState::ABORTED);
 }
 for(auto state:{MediaPlayerResponseState::IDLE,MediaPlayerResponseState::FINISHED,
                 MediaPlayerResponseState::ABORTED}){
  Voice v;v.state_=State::IDLE;v.media_player_response_state_=state;v.stop();assert(v.completions.empty());
 }
 Voice queued;queued.state_=State::STOP_MICROPHONE;
 queued.media_player_response_state_=MediaPlayerResponseState::URL_SENT;
 queued.stop();queued.stop();
 assert(queued.player.stop_requests==1);
 assert(queued.completions.size()==1 && !queued.completions.front());
 Voice finished;finished.media_player_response_state_=MediaPlayerResponseState::FINISHED;
 finished.stop();finished.stop();
 assert(finished.completions.size()==1 && finished.completions.front());
 assert(finished.player.stop_requests==0);
}
'''
    cpp = tmp_path / "stop_response.cpp"
    cpp.write_text(stub + body + checks)
    binary = tmp_path / "stop_response"
    subprocess.run(
        ["g++", "-std=c++20", str(cpp), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True)
