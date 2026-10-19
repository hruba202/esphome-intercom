"""Compare the real shared playback queue with an independent per-frame oracle."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_source_credit_matches_independent_sample_timeline(tmp_path):
    source = tmp_path / "timeline.cpp"
    source.write_text(r'''
#include "playback_queue.h"
#include <cassert>
#include <deque>
#include <random>
using esphome::mixer_speaker::PlaybackQueue;
int main() {
 PlaybackQueue queue;
 assert(queue.append(1,100));assert(queue.append(2,100));assert(queue.append(1,100));
 auto first=queue.consume(150);assert(first[0].frames==100 && first[0].trailing_frames==50);
 assert(first[1].frames==50 && first[1].trailing_frames==0);
 // Credits remain owned until the callback returns.
 assert(queue.pending_frames(0)==200);
 queue.acknowledge(0,first[0].frames);queue.acknowledge(1,first[1].frames);
 queue.discard_source(0);assert(queue.pending_frames(0)==0);
 auto rest=queue.consume(150);assert(rest[0].frames==0 && rest[1].frames==50);
 queue.acknowledge(1,50);assert(queue.pending_frames()==0);
 for(unsigned i=0;i<32;++i)assert(queue.append(1+(i%2),1));
 assert(queue.full());assert(!queue.append(4,1));queue.clear();
 std::deque<uint8_t> wire;std::mt19937 random(42);
 auto complete=[&](unsigned count){
  unsigned expected[8]={},trailing[8]={};
  for(unsigned n=0;n<count;++n){
   unsigned mask=wire.front();wire.pop_front();
   for(unsigned i=0;i<8;++i)if(mask&(1U<<i)){++expected[i];trailing[i]=count-n-1;}
  }
  auto actual=queue.consume(count);
  for(unsigned i=0;i<8;++i){
   assert(actual[i].frames==expected[i]);
   if(expected[i])assert(actual[i].trailing_frames==trailing[i]);
   queue.acknowledge(i,actual[i].frames);
  }
 };
 for(unsigned step=0;step<10000;++step){
  unsigned count=1+random()%31;uint8_t mask=random()%256;
  if(!wire.empty() && (random()%2 || queue.full()))complete(std::min<unsigned>(count,wire.size()));
  else if(queue.append(mask,count))for(unsigned i=0;i<count;++i)wire.push_back(mask);
  if(step%137==0){
   unsigned abandoned=random()%8;queue.discard_source(abandoned);
   for(auto &m:wire)m&=~(1U<<abandoned);
  }
 }
 while(!wire.empty())complete(std::min<unsigned>(17,wire.size()));
 assert(queue.pending_frames()==0);
 for(unsigned i=0;i<8;++i)assert(queue.pending_frames(i)==0);
}
''')
    binary = tmp_path / "timeline"
    subprocess.run([
        "g++", "-std=c++17", "-fsanitize=address,undefined", "-I",
        str(ROOT / "esphome/components/mixer/speaker"), str(source), "-o", str(binary),
    ], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
