#pragma once

// Host test input only. This header is never part of an ESP device profile.
// LVGL 9.5.0 pointer read_cb drives actual hit testing, gestures and scrolling.
// The host clock/tick callback stays owned by ESPHome; no LV_EVENT_GESTURE is injected.
#ifdef USE_HOST

#include <lvgl.h>

#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <thread>

namespace esphome::ui_fixture {

class PointerProbe {
 public:
  using WatchHandle = size_t;

  explicit PointerProbe(lv_display_t *display) : display_(display) {
    assert(display != nullptr);
    this->input_ = lv_indev_create();
    assert(this->input_ != nullptr);
    lv_indev_set_type(this->input_, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(this->input_, display);
    lv_indev_set_user_data(this->input_, this);
    lv_indev_set_read_cb(this->input_, &PointerProbe::read_);
  }

  PointerProbe(const PointerProbe &) = delete;
  PointerProbe &operator=(const PointerProbe &) = delete;

  ~PointerProbe() {
    for (auto &watch : this->watches_) {
      if (watch.object != nullptr && watch.descriptor != nullptr)
        lv_obj_remove_event_dsc(watch.object, watch.descriptor);
    }
    lv_indev_delete(this->input_);
  }

  // All calls belong to the host LVGL/main thread. A pump advances real host
  // time and processes input/timers; it is intentionally a test loop, not an
  // event source or polling worker added to production firmware.
  void pump(uint32_t milliseconds = 40) {
    using Clock = std::chrono::steady_clock;
    // The read timer created by LVGL uses LV_DEF_REFR_PERIOD. Let it deliver
    // input naturally; forced reads between samples turn continuous motion
    // into repeated zero vectors and can reset LVGL's gesture accumulator.
    milliseconds = std::max<uint32_t>(milliseconds, LV_DEF_REFR_PERIOD + 5U);
    const auto deadline = Clock::now() + std::chrono::milliseconds(milliseconds);
    const auto previous_reads = this->reads_;
    bool animation_observed = false;
    do {
      animation_observed |= lv_display_get_screen_prev(this->display_) != nullptr;
      lv_timer_handler();
      animation_observed |= lv_display_get_screen_prev(this->display_) != nullptr;
      const auto now = Clock::now();
      if (now >= deadline)
        break;
      std::this_thread::sleep_until(std::min(deadline, now + std::chrono::milliseconds(1)));
    } while (true);
    this->last_pump_reads_ = this->reads_ - previous_reads;
    this->last_pump_animation_ = animation_observed;
    if (this->last_pump_reads_ == 0 && animation_observed) {
      // LVGL intentionally returns before read_cb while prev_scr is active.
      // Keep the zero count visible; do not simulate a read or weaken the
      // assertion when no real screen transition was observed.
      ++this->animation_suppressed_pumps_;
    } else {
      assert(this->last_pump_reads_ != 0);
    }
  }

  void press(int32_t x, int32_t y, uint32_t milliseconds = 40) {
    this->point_ = {x, y};
    this->state_ = LV_INDEV_STATE_PRESSED;
    this->pump(milliseconds);
  }

  void move(int32_t x, int32_t y, uint32_t milliseconds = 30) {
    this->motion_from_ = this->point_;
    this->motion_to_ = {x, y};
    this->motion_start_ = std::chrono::steady_clock::now();
    this->motion_ms_ = std::max<uint32_t>(1, milliseconds);
    this->moving_ = true;
    this->pump(milliseconds);
    this->moving_ = false;
    this->point_ = this->motion_to_;
    // Latch the endpoint as PRESSED through a real input-timer read before
    // release. Otherwise a short stroke can lose its last movement sample.
    this->pump(LV_DEF_REFR_PERIOD + 5U);
  }

  void release(uint32_t milliseconds = 80) {
    this->state_ = LV_INDEV_STATE_RELEASED;
    this->pump(milliseconds);
  }

  void tap(int32_t x, int32_t y) {
    this->press(x, y);
    this->release();
  }

  void swipe(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint32_t steps = 8,
             uint32_t step_ms = 25) {
    assert(steps != 0 && steps <= 100 && step_ms != 0);
    this->press(x0, y0);
    this->move(x1, y1, steps * step_ms);
    this->release();
  }

  // Storage remains stable when adding watches. DELETE is observed so the
  // destructor never removes callbacks from an object already deleted by LVGL.
  WatchHandle watch(lv_obj_t *object, lv_event_code_t event) {
    assert(object != nullptr);
    this->watches_.push_back({object, nullptr, event, 0});
    auto &watch = this->watches_.back();
    watch.descriptor = lv_obj_add_event_cb(object, &PointerProbe::event_, LV_EVENT_ALL, &watch);
    assert(watch.descriptor != nullptr);
    return this->watches_.size() - 1;
  }

  uint64_t count(WatchHandle handle) const {
    assert(handle < this->watches_.size());
    return this->watches_[handle].count;
  }

  uint64_t read_count() const { return this->reads_; }
  uint64_t animation_suppressed_pumps() const { return this->animation_suppressed_pumps_; }
  uint64_t last_pump_reads() const { return this->last_pump_reads_; }
  bool last_pump_animation_observed() const { return this->last_pump_animation_; }

 private:
  struct Watch {
    lv_obj_t *object;
    lv_event_dsc_t *descriptor;
    lv_event_code_t event;
    uint64_t count;
  };

  static void read_(lv_indev_t *input, lv_indev_data_t *data) {
    auto *self = static_cast<PointerProbe *>(lv_indev_get_user_data(input));
    if (self->moving_) {
      const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
          std::chrono::steady_clock::now() - self->motion_start_).count();
      const double ratio = std::min(1.0, static_cast<double>(elapsed) / (self->motion_ms_ * 1000.0));
      self->point_.x = self->motion_from_.x + static_cast<int32_t>(std::lround(
          (static_cast<int64_t>(self->motion_to_.x) - self->motion_from_.x) * ratio));
      self->point_.y = self->motion_from_.y + static_cast<int32_t>(std::lround(
          (static_cast<int64_t>(self->motion_to_.y) - self->motion_from_.y) * ratio));
    }
    data->point = self->point_;
    data->state = self->state_;
    data->continue_reading = false;
    ++self->reads_;
  }

  static void event_(lv_event_t *event) {
    auto *watch = static_cast<Watch *>(lv_event_get_user_data(event));
    const auto code = lv_event_get_code(event);
    if (watch->event == LV_EVENT_ALL || code == watch->event)
      ++watch->count;
    if (code == LV_EVENT_DELETE && lv_event_get_target(event) == watch->object) {
      watch->object = nullptr;
      watch->descriptor = nullptr;
    }
  }

  lv_indev_t *input_{nullptr};
  lv_display_t *display_;
  lv_point_t point_{};
  lv_indev_state_t state_{LV_INDEV_STATE_RELEASED};
  lv_point_t motion_from_{};
  lv_point_t motion_to_{};
  std::chrono::steady_clock::time_point motion_start_{};
  uint32_t motion_ms_{0};
  bool moving_{false};
  uint64_t reads_{0};
  uint64_t animation_suppressed_pumps_{0};
  uint64_t last_pump_reads_{0};
  bool last_pump_animation_{false};
  std::deque<Watch> watches_;
};

}  // namespace esphome::ui_fixture

#endif  // USE_HOST
