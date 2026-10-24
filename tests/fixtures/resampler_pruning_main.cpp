#include "audio_resampler.h"
#include <gain.h>
#include <mixer.h>
#include <pcm_convert.h>
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

unsigned ram_allocations = 0;
uint64_t convolution_calls = 0;
uint64_t projected_frames = 0;
extern "C" int __real_dsps_dotprod_f32_ansi(const float *, const float *, float *, int);
extern "C" int __wrap_dsps_dotprod_f32_ansi(const float *a, const float *b, float *out, int n) {
  ++convolution_calls;
  return __real_dsps_dotprod_f32_ansi(a, b, out, n);
}
extern "C" void __real__ZN14esp_audio_libs11pcm_convert11copy_framesEPKhPhhhhhj(
    const uint8_t *, uint8_t *, uint8_t, uint8_t, uint8_t, uint8_t, uint32_t);
extern "C" void __wrap__ZN14esp_audio_libs11pcm_convert11copy_framesEPKhPhhhhhj(
    const uint8_t *in, uint8_t *out, uint8_t ib, uint8_t ic, uint8_t ob, uint8_t oc, uint32_t n) {
  if (ic == 2 && oc == 1) projected_frames += n;
  __real__ZN14esp_audio_libs11pcm_convert11copy_framesEPKhPhhhhhj(in, out, ib, ic, ob, oc, n);
}

namespace esphome::mixer_speaker {
struct SourceSpeaker {
  audio::AudioStreamInfo audio_stream_info_{16, 2, 48000};
  esp_audio_libs::gain::GainRamp ducking_ramp_;
  size_t process_data_from_source(std::shared_ptr<audio::RingBufferAudioSource> &, TickType_t);
  void apply_ducking(uint8_t, uint32_t);
};
// @PRODUCTION_DUCK_METHODS@
}

using namespace esphome;
struct Sink : speaker::Speaker {
  std::vector<uint8_t> bytes;
  std::vector<size_t> accepted;
  bool partial;
  size_t calls = 0;
  explicit Sink(bool p) : partial(p) {}
  size_t play(const uint8_t *data, size_t length, TickType_t) override {
    constexpr size_t sizes[] = {0, 1, 3, 31, 7, 127, 2048};
    size_t n = partial ? std::min(length, sizes[calls++ % 7]) : length;
    bytes.insert(bytes.end(), data, data + n);
    accepted.push_back(n);
    return n;
  }
};
template<class T> esp_err_t start(T &value, audio::AudioStreamInfo &in, audio::AudioStreamInfo &out, bool mono, unsigned taps = 16, unsigned filters = 16) {
  // The pre-fix wrapper still compiles: missing pruning manifests as excess
  // real convolution work, rather than a collection or compiler failure.
  if constexpr (requires { value.start(in, out, taps, filters, mono); }) return value.start(in, out, taps, filters, mono);
  return value.start(in, out, taps, filters);
}
struct Result {
  std::vector<uint8_t> pcm;
  std::vector<size_t> accepted;
  uint64_t convolutions;
  uint64_t projected;
  unsigned startup_allocations;
};
Result run(const std::vector<int16_t> &samples, unsigned rate, bool mono, bool partial, unsigned channels = 2, unsigned taps = 16, unsigned filters = 16) {
  auto ring = std::make_shared<ring_buffer::RingBuffer>();
  ring->bytes.resize(samples.size() * 2);
  std::memcpy(ring->bytes.data(), samples.data(), ring->bytes.size());
  ring->read_limit = partial ? 113 : 512;
  Sink sink(partial);
  audio::AudioStreamInfo in(16, channels, rate), out(16, channels, 48000);
  unsigned before = ram_allocations;
  audio::AudioResampler resampler(in.ms_to_bytes(50), out.ms_to_bytes(50));
  assert(start(resampler, in, out, mono, taps, filters) == ESP_OK);
  std::weak_ptr<ring_buffer::RingBuffer> source = ring;
  assert(resampler.add_source(source) == ESP_OK && resampler.add_sink(&sink) == ESP_OK);
  const unsigned startup_allocations = ram_allocations - before;
  const unsigned allocated = ram_allocations;
  convolution_calls = projected_frames = 0;
  bool finished = false;
  for (unsigned step = 0; step < 200000; ++step) {
    int32_t differential = 0;
    auto state = resampler.resample(true, &differential);
    assert(ram_allocations == allocated);  // No allocations on write/retry.
    assert(state != audio::AudioResamplerState::FAILED);
    if (state == audio::AudioResamplerState::FINISHED) { finished = true; break; }
  }
  assert(finished && ring->available() == 0 && sink.bytes.size() % (channels * 2) == 0);
  // The ring repeatedly reuses the exact same address and length, containing
  // different PCM. A pointer/length cache key would replay old samples here.
  assert(ring->reused_exposures > 10);
  return {sink.bytes, sink.accepted, convolution_calls, projected_frames, startup_allocations};
}

std::vector<uint8_t> render(const std::vector<uint8_t> &pcm, bool duck, bool mix) {
  auto ring = std::make_shared<ring_buffer::RingBuffer>();
  ring->bytes = pcm;
  ring->read_limit = 196;
  auto source = std::shared_ptr<audio::RingBufferAudioSource>(audio::RingBufferAudioSource::create(ring, 196, 4));
  mixer_speaker::SourceSpeaker speaker;
  if (duck) speaker.apply_ducking(19, 7);
  std::vector<uint8_t> result;
  unsigned passes = 0;
  while (source->has_buffered_data()) {
    speaker.process_data_from_source(source, 0);
    size_t frames = std::min<size_t>(source->available() / 4, 17);
    if (!frames) continue;
    std::vector<uint8_t> mono(frames * 2);
    if (mix) {
      std::vector<int16_t> other(frames, 20000);
      esp_audio_libs::mixer::mix_frames(source->data(), 2, 2,
          reinterpret_cast<const uint8_t *>(other.data()), 2, 1, mono.data(), 2, 1, frames);
    } else {
      esp_audio_libs::pcm_convert::copy_frames(source->data(), mono.data(), 2, 2, 2, 1, frames);
    }
    result.insert(result.end(), mono.begin(), mono.end());
    source->consume(frames * 4);
    if (duck && ++passes == 9) speaker.apply_ducking(0, 11);
  }
  return result;
}

int main(int argc, char **argv) {
  assert(argc == 6);
  unsigned rate = std::strtoul(argv[1], nullptr, 10);
  bool partial = std::strtoul(argv[2], nullptr, 10);
  unsigned channels = std::strtoul(argv[3], nullptr, 10);
  unsigned taps = std::strtoul(argv[4], nullptr, 10);
  unsigned filters = std::strtoul(argv[5], nullptr, 10);
  std::mt19937 random(731);
  for (unsigned pattern = 0; pattern < 3; ++pattern) {
    std::vector<int16_t> samples(4096 * channels);
    for (size_t f = 0; f < samples.size() / channels; ++f) {
      samples[f * channels] = pattern == 0 ? int16_t(random()) : (pattern == 1 ? 0 : 32767);
      if (channels == 2)
        samples[f * channels + 1] = pattern == 0 ? int16_t(random()) : (pattern == 1 ? 32767 : -32768);
    }
    auto baseline = run(samples, rate, false, partial, channels, taps, filters);
    auto optimized = run(samples, rate, true, partial, channels, taps, filters);
    assert(baseline.pcm.size() == optimized.pcm.size());
    assert(baseline.accepted == optimized.accepted);  // Same byte/frame credits.
    if (channels == 2) {
      for (bool duck : {false, true}) for (bool mix : {false, true})
        assert(render(baseline.pcm, duck, mix) == render(optimized.pcm, duck, mix));
    } else {
      assert(baseline.pcm == optimized.pcm);
    }
    // Stop with pending PCM, then start a new acquisition. The real wrapper
    // destructor must release its held source and scratch/history owners.
    {
      auto interrupted = std::make_shared<ring_buffer::RingBuffer>();
      interrupted->bytes.resize(samples.size() * 2);
      std::memcpy(interrupted->bytes.data(), samples.data(), interrupted->bytes.size());
      {
        audio::AudioStreamInfo in(16, channels, rate), out(16, channels, 48000);
        audio::AudioResampler stopped(in.ms_to_bytes(50), out.ms_to_bytes(50));
        Sink blocked(true);
        std::weak_ptr<ring_buffer::RingBuffer> source = interrupted;
        assert(start(stopped, in, out, true, taps, filters) == ESP_OK);
        assert(stopped.add_source(source) == ESP_OK && stopped.add_sink(&blocked) == ESP_OK);
        int32_t differential = 0;
        stopped.resample(false, &differential);
      }
      // Destruction releases the acquired item. The ring still owns bytes
      // not yet read, until its task owner discards the whole ring on stop.
      assert(!interrupted->acquired && interrupted.use_count() == 1);
    }
    auto restarted = run(samples, rate, true, partial, channels, taps, filters);
    assert(restarted.pcm == optimized.pcm && restarted.accepted == optimized.accepted);
    std::printf("rate=%u partial=%u pattern=%u PCM_and_credits_equal=1 baseline_FIR=%llu pruned_FIR=%llu projected=%llu\n",
                rate, partial, pattern, (unsigned long long) baseline.convolutions,
                (unsigned long long) optimized.convolutions, (unsigned long long) optimized.projected);
    std::fflush(stdout);
    if (rate != 48000 && channels == 2) {
      assert(optimized.convolutions > 0 && optimized.convolutions * 2 == baseline.convolutions);
      assert(optimized.projected == samples.size() / 2);  // Never reproject on retry.
    } else {
      assert(optimized.convolutions == baseline.convolutions && optimized.projected == 0);
      assert(optimized.startup_allocations == baseline.startup_allocations);
    }
  }
}
