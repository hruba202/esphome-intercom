"""Compile real audio primitives with explicit host allocation and ring adapters."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def test_audio_conversions_and_buffer_lifetime(tmp_path):
    core = tmp_path / "esphome/core"
    core.mkdir(parents=True)
    (core / "defines.h").write_text("#pragma once\n")
    (core / "helpers.h").write_text(r'''#pragma once
#include <algorithm>
#include <cstdlib>
#include <memory>
#include <cstring>
#include <strings.h>
#define ESPDEPRECATED(message, version)
namespace esphome {
using std::make_unique;
template<class T> struct RAMAllocator {
 T *allocate(size_t n) { return static_cast<T*>(std::malloc(n*sizeof(T))); }
 T *reallocate(T *p,size_t n) { return static_cast<T*>(std::realloc(p,n*sizeof(T))); }
 void deallocate(T *p,size_t) { std::free(p); }
};
inline bool str_endswith_ignore_case(const char *s,const char *tail) {
 size_t n=std::strlen(s),k=std::strlen(tail);return n>=k && strcasecmp(s+n-k,tail)==0;
}
}
''')
    ring = tmp_path / "esphome/components/ring_buffer"
    ring.mkdir(parents=True)
    (ring / "ring_buffer.h").write_text(r'''#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>
using TickType_t = unsigned;
namespace esphome::ring_buffer {
class RingBuffer {
 public:
 std::vector<uint8_t> bytes; bool acquired=false; unsigned released=0;
 size_t available() const { return acquired?0:bytes.size(); }
 void reset() { bytes.clear(); acquired=false; }
 size_t write_without_replacement(void *p,size_t n,TickType_t) {
   auto *b=static_cast<uint8_t*>(p);bytes.insert(bytes.end(),b,b+n);return n;
 }
 size_t read(void *p,size_t n,TickType_t) {
   n=std::min(n,bytes.size());std::memcpy(p,bytes.data(),n);bytes.erase(bytes.begin(),bytes.begin()+n);return n;
 }
 void *receive_acquire(size_t &n,size_t,TickType_t) {
   if(acquired || bytes.empty())return nullptr;
   acquired=true;n=bytes.size();return bytes.data();
 }
 void receive_release(void*) { acquired=false;bytes.clear();++released; }
};
}
''')
    cpp = tmp_path / "audio_check.cpp"
    cpp.write_text(r'''
#include "audio.h"
#include "audio_transfer_buffer.h"
#include <cassert>
#include <cstring>
using namespace esphome;
int main() {
 audio::AudioStreamInfo info(16,2,48000);
 assert(info.frames_to_microseconds(48000)==1000000);
 assert(info.ms_to_frames(120000)==5760000);
 assert(info.ms_to_samples(120000)==11520000);
 assert(info.ms_to_bytes(120000)==23040000);
 assert(info.bytes_to_ms(23040000)==120000);
 uint32_t frames=5760000;assert(info.frames_to_milliseconds_with_remainder(&frames)==120000);
 assert(frames==0);
 auto ring=std::make_shared<ring_buffer::RingBuffer>();
 auto source=audio::RingBufferAudioSource::create(ring,64,4);
 ring->bytes={1,2};assert(source->fill(0)==0);
 assert(source->expose_padded_partial(2)==2);
 assert(source->available()==4);
 const uint8_t expected[]={1,2,0,0};assert(std::memcmp(source->data(),expected,4)==0);
 source->consume(4);assert(source->expose_padded_partial(2)==0);
 ring->bytes={3};assert(source->fill(0,false)==0);
 assert(source->expose_padded_partial(2)==0); // Never expose half a PCM frame.
 ring->bytes={4,5,6};assert(source->fill(0,false)==4);
 const uint8_t joined[]={3,4,5,6};assert(std::memcmp(source->data(),joined,4)==0);
 source->consume(4);source->clear_buffered_data();
 assert(!source->has_buffered_data() && !ring->acquired);
 auto sink=audio::AudioSinkTransferBuffer::create(8);sink->set_sink(ring);
 std::memcpy(sink->get_buffer_end(),joined,4);sink->increase_buffer_length(4);
 assert(sink->transfer_data_to_sink(0)==4 && sink->available()==0);
 assert(ring->bytes==std::vector<uint8_t>({3,4,5,6}));
 sink->clear_buffered_data();assert(ring->bytes.empty());
}
''')
    component = ROOT / "esphome/components/audio"
    executable = tmp_path / "audio_check"
    subprocess.run([
        "g++", "-std=c++20", "-Wall", "-Wextra", "-fsanitize=address,undefined",
        "-I", str(tmp_path), "-I", str(component), str(cpp),
        str(component / "audio.cpp"), str(component / "audio_transfer_buffer.cpp"),
        "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
