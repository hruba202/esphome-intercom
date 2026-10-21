"""Source completion drains normally; maintenance owns hard cancellation."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_source_completion_preserves_the_pipeline_owners_stop_request(tmp_path):
    text=(ROOT/'esphome/components/speaker_source/speaker_source_media_player.cpp').read_text()
    begin=text.index('void SpeakerSourceMediaPlayer::handle_media_state_changed_(')
    end=text.index('// THREAD CONTEXT:',begin)
    code=r'''
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
namespace media_source {
enum class MediaSourceState {IDLE,PLAYING,PAUSED,ERROR};struct MediaSource {};
}
struct Sink {
 bool stopped=false;unsigned stops=0,finishes=0;
 void stop(){++stops;stopped=true;}
 // FINISH does not undo the owner's earlier STOP request.
 void finish(){++finishes;}
};
struct PipelineContext {
 std::atomic<media_source::MediaSource*> active_source{nullptr};
 media_source::MediaSource *stopping_source=nullptr,*pending_source=nullptr,*last_source=nullptr;
 Sink *speaker=nullptr;
};
struct MediaPlayerControlCommand {enum {PLAYLIST_ADVANCE};};
struct SpeakerSourceMediaPlayer {
 std::array<PipelineContext,2> pipelines_;unsigned advances=0;
 void queue_command_(int,uint8_t){++advances;}
 void handle_media_state_changed_(uint8_t,media_source::MediaSource*,media_source::MediaSourceState);
};
''' + text[begin:end] + r'''
int main(){
 SpeakerSourceMediaPlayer player;media_source::MediaSource cancelled,natural,unrelated;
 Sink stopped,finished;auto &p=player.pipelines_[0];auto &q=player.pipelines_[1];
 // The composed maintenance sequence stops the pipeline before shared I2S.
 // Late decoder IDLE is a completion notification, not another cancellation owner.
 stopped.stop();
 p.active_source=&cancelled;p.stopping_source=&cancelled;p.speaker=&stopped;
 q.active_source=&natural;q.speaker=&finished;
 player.handle_media_state_changed_(0,&cancelled,media_source::MediaSourceState::IDLE);
 assert(stopped.stopped && stopped.stops==1 && stopped.finishes==1);
 assert(p.active_source==nullptr && p.stopping_source==nullptr && player.advances==0);
 // A duplicate or an unrelated source cannot stop another generation/pipeline.
 player.handle_media_state_changed_(0,&cancelled,media_source::MediaSourceState::IDLE);
 player.handle_media_state_changed_(1,&unrelated,media_source::MediaSourceState::IDLE);
 assert(stopped.stops==1 && stopped.finishes==1 && finished.stops==0 && finished.finishes==0);
 player.handle_media_state_changed_(1,&natural,media_source::MediaSourceState::IDLE);
 assert(finished.finishes==1 && finished.stops==0 && player.advances==1);
 // The same pipeline can start a new source immediately after cancellation.
 p.pending_source=&natural;
 player.handle_media_state_changed_(0,&natural,media_source::MediaSourceState::PLAYING);
 assert(p.active_source==&natural && p.pending_source==nullptr && stopped.stopped);
}
'''
    source = tmp_path / 'stop.cpp'
    source.write_text(code)
    binary = tmp_path / 'stop'
    subprocess.run(['g++','-std=c++17',str(source),'-o',str(binary)],check=True,capture_output=True,text=True)
    result=subprocess.run(['bash','-c','ulimit -c 0; exec "$1"','bash',str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
