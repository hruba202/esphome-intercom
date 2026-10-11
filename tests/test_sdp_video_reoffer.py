"""Video updates retain the established audio and telephone-event contract."""

import re
import pytest
from .voip_phase1_support import sdp
from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS


def audio_section(body):
    if isinstance(body, bytes):
        body = body.decode()
    return re.search(r'^m=audio .*?(?=^m=|\Z)', body, re.M | re.S).group(0)


@pytest.mark.parametrize('initial_video', [False, True])
@pytest.mark.parametrize('direction,port', [('sendrecv', 41002), ('recvonly', 41002), ('inactive', 0)])
def test_video_update_preserves_audio_payloads_dtmf_and_attributes(initial_video, direction, port):
    initial = sdp.build_offer_directional(
        '192.0.2.10', '192.0.2.10', 41000,
        list(HA_TRUNK_AUDIO_FORMATS), list(HA_TRUNK_AUDIO_FORMATS),
        include_common_codecs=True,
        video_port=41002 if initial_video else 0,
        video_formats=sdp.DEFAULT_VIDEO_FORMATS if initial_video else (),
    )
    initial = initial.replace('a=ptime:20', 'a=rtcp:41001 IN IP4 192.0.2.10\r\na=ptime:20')
    changed = sdp.build_video_reoffer(initial, video_port=port,
                                    video_formats=sdp.DEFAULT_VIDEO_FORMATS, video_direction=direction)
    assert audio_section(changed) == audio_section(initial)
    assert sdp.offered_dtmf_formats(changed) == sdp.offered_dtmf_formats(initial)
    assert len(re.findall(r'^m=video ', changed, re.M)) == 1
    assert f'm=video {port} RTP/AVP' in changed
    assert changed.split('m=audio', 1)[0] == initial.split('m=audio', 1)[0]
    restored = sdp.build_video_reoffer(changed, video_port=41002,
                                     video_formats=sdp.DEFAULT_VIDEO_FORMATS, video_direction='sendrecv')
    assert audio_section(restored) == audio_section(initial)
