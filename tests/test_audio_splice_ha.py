"""Audio handoff preserves transport addresses, RTP clocks and ownership."""

import struct
import unittest
import pytest

from custom_components.voip_stack.core import audio_format, sdp, rtp
from custom_components.voip_stack import sip_rtp_bridge

pytestmark = pytest.mark.ha


class Transport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []
        self.closed = 0
        self.fail_next = False

    def get_protocol(self):
        return self.protocol

    def set_protocol(self, protocol):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected protocol handoff failure")
        self.protocol = protocol

    def is_closing(self):
        return bool(self.closed)

    def close(self):
        self.closed += 1

    def sendto(self, data, address):
        assert not self.closed
        self.sent.append((data, address))


class AudioSpliceTest(unittest.IsolatedAsyncioTestCase):
    def relay(self, index, releases):
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        result = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.1", 10000 + index * 10, 96, fmt, rtp_format=wire),
            right=sip_rtp_bridge.RtpPeer("127.0.0.1", 10002 + index * 10, 96, fmt, rtp_format=wire),
            left_port=20000 + index * 10, right_port=20002 + index * 10,
            on_release=lambda ports: releases.append((index, ports)),
        )
        for side in ("left", "right"):
            protocol = sip_rtp_bridge._RelayProtocol(result, side)
            transport = Transport(protocol)
            protocol.connection_made(transport)
            setattr(result, f"{side}_transport", transport)
        return result

    async def test_splice_keeps_sockets_and_duplex_samples_then_releases_both_reservations(self):
        released = []
        first, second = self.relay(0, released), self.relay(1, released)
        a, b1, b2, c = first.left_transport, first.right_transport, second.left_transport, second.right_transport
        a_clock, c_clock = first.left.ssrc, second.right.ssrc
        combined = sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        await first.stop()
        await second.stop()
        self.assertEqual(released, [])
        self.assertEqual((a.closed, b1.closed, b2.closed, c.closed), (0, 1, 1, 0))
        for transport, peer, target, sample in (
            (a, combined.left, c, 1700), (c, combined.right, a, -2100),
        ):
            payload = struct.pack("!320h", *([sample] * 320))
            transport.protocol.datagram_received(
                rtp.build_packet(rtp.RtpPacket(96, 1, 320, 123, payload)), (peer.host, peer.port),
            )
            self.assertTrue(target.sent)
            packet = rtp.parse_packet(target.sent[-1][0])
            self.assertEqual(packet.payload, payload)
            self.assertEqual(packet.ssrc, c_clock if target is c else a_clock)
        self.assertEqual(b1.sent, [])
        self.assertEqual(b2.sent, [])
        await combined.stop()
        await combined.stop()
        self.assertEqual(released, [(0, (20000, 20002)), (1, (20010, 20012))])
        self.assertEqual((a.closed, c.closed), (1, 1))

    async def test_failed_protocol_handoff_restores_both_original_relays(self):
        released = []
        first, second = self.relay(0, released), self.relay(1, released)
        a, c = first.left_transport, second.right_transport
        old_a, old_c = a.protocol, c.protocol
        c.fail_next = True
        with self.assertRaisesRegex(RuntimeError, "handoff failure"):
            sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        self.assertIs(first.left_transport, a)
        self.assertIs(second.right_transport, c)
        self.assertIs(a.protocol, old_a)
        self.assertIs(c.protocol, old_c)
        self.assertFalse(a.closed or c.closed)
        combined = sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        await first.stop()
        await second.stop()
        await combined.stop()
        self.assertEqual(len(released), 2)
