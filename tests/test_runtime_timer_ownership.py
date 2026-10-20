"""Execute the package predicates against independent timer/ringtone scenarios."""

from pathlib import Path
import subprocess

from tests.test_runtime_ui_connectivity import _load, _scripts, PackageLoader
from tests.runtime_observer_fixture import source_event_body
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_timer_cancellation_and_announcement_priority(tmp_path):
    timer_events = _load("packages/voice_assistant/timer_events.yaml")
    assert timer_events["runtime_controller"]["observe"]["timers"] == "va"
    assert "on_timer_cancelled" not in timer_events["voice_assistant"]
    timer = _scripts(_load("packages/runtime/timer_alarm_orchestration.yaml"))[
        "timer_alarm_loop"
    ]
    play_guard = timer["then"][0]["while"]["then"][0]["if"]["condition"]["lambda"]
    controller = (
        ROOT
        / "packages/features/timers/runtime.yaml"
    )
    policy = yaml.load(controller.read_text(), Loader=PackageLoader)[
        "runtime_controller"
    ]["policies"]["timer_alarm"]
    stop_guard = policy["values"]["stop"]["then"][-1]["if"]["condition"]["lambda"]
    source = r"""
#include <cassert>
#include <cstring>
#include <string>
#include <vector>
#define id(x) (x)
#define USE_RUNTIME_CONTROLLER_VOICE_ASSISTANT
struct Timer { std::string id; } timer;
struct Voice { std::vector<Timer> timers; const auto &get_timers() const { return timers; } } va;
struct Runtime {
 bool ringing=false, draining=false, voice=false;
 bool timer_active=false;
 std::vector<std::string> events;
 void observe_activity(const char *, bool active) {timer_active=active;}
 void observe_event(const char *event) {events.emplace_back(event);}
 const char *get_policy(const char *) const { return ringing ? "play" : "stop"; }
 bool is_activity_active(const char *name) const {
   return std::string(name)=="ringtone_draining" ? draining : voice;
 }
} runtime;
struct RuntimeNetworkObservers {
 Runtime *runtime_=&runtime; Voice *voice_assistant_=&va;
 void source_event(const char *, const std::string &);
} observer;
"""
    source += source_event_body()
    source += "bool can_play() {\n" + play_guard + "\n}\n"
    source += "bool can_stop() {\n" + stop_guard + "\n}\n"
    source += r"""
int main() {
 timer.id="a"; va.timers={{"a"}};
 observer.source_event("timer_cancelled",timer.id);assert(!runtime.timer_active);
 va.timers={{"a"},{"b"}};
 observer.source_event("timer_cancelled",timer.id);assert(runtime.timer_active);
 observer.source_event("timer_finished",timer.id);assert(runtime.timer_active);
 assert(runtime.events.back()=="timer_finished");
 for (int mask=0;mask<8;mask++) {
   runtime.ringing=mask&1;runtime.draining=mask&2;runtime.voice=mask&4;
   assert(can_play()==(!(mask&1) && !(mask&2)));
   assert(can_stop()==(mask==0));
 }
}
"""
    cpp = tmp_path / "ownership.cpp"
    cpp.write_text(source)
    binary = tmp_path / "ownership"
    subprocess.run(
        ["g++", "-std=c++17", "-Wall", "-Werror", str(cpp), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True)


def test_ringtone_stop_releases_player_playlist():
    stop = _scripts(_load("packages/runtime/voip_ringtone_orchestration.yaml"))[
        "voip_stop_ringtone"
    ]
    owned_stop = stop["then"][0]["if"]
    assert "ringtone_draining" in owned_stop["condition"]["lambda"]
    assert owned_stop["then"][0] == {"script.stop": "voip_ringing_loop"}
    assert owned_stop["then"][1] == {
        "media_player.stop": {"id": "speaker_media_player", "announcement": True}
    }
