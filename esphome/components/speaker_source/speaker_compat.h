#pragma once

#include "esphome/components/speaker/speaker.h"

namespace esphome::speaker_source {

// Same threshold as the recorded upstream speaker API. Older ESPHome lacks
// both the named constant and native DAC silence handling.
static constexpr float SILENT_VOLUME_THRESHOLD = 0.001f;

template<typename Speaker> struct SpeakerCompatibility : Speaker {
  static constexpr bool NATIVE_SILENCE = requires(SpeakerCompatibility &value) { value.is_silent_(); };
};

}  // namespace esphome::speaker_source
