"""Cross-repository contracts for maintained ESPHome audio profiles."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_profiles_do_not_use_removed_blocking_audio_stack_action() -> None:
    offenders: list[str] = []
    for base in (ROOT / "packages", ROOT / "yamls"):
        for path in base.rglob("*.yaml"):
            if ".esphome" in path.parts:
                continue
            if "esp_audio_stack.stop_and_wait" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_ota_waits_for_audio_stack_idle_without_blocking_main_loop() -> None:
    from tests.native_profile import profile
    from tests.test_package_composition import maintenance_begin_actions

    for relative in (
        'full-experience/single-bus/waveshare-s3-full-afe.yaml',
        'full-experience/single-bus/spotpear-ball-v2-full-afe.yaml',
    ):
        steps = maintenance_begin_actions(profile(relative))
        sink_index = next(i for i, step in enumerate(steps) if 'speaker.stop' in step)
        stop_index = next(i for i, step in enumerate(steps) if 'esp_audio_stack.stop' in step)
        wait_index = next(i for i, step in enumerate(steps)
                          if 'esp_audio_stack.is_idle' in step.get('wait_until', {}).get('condition', {}))
        assert sink_index < stop_index < wait_index
        assert steps[sink_index]['speaker.stop'] == 'hw_speaker'
        assert steps[stop_index]['esp_audio_stack.stop'] in (None, {})
        assert steps[wait_index]['wait_until']['timeout'] == '2s'
        # Nothing may be deferred until after OTA's blocking data phase.
        assert wait_index == len(steps) - 1
