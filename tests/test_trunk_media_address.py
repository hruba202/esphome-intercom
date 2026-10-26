"""Outbound trunk media addressing remains independent of signaling and LAN calls."""

from .voip_phase1_support import asyncio, sdp, sip, sip_client, types, unittest, _load_intercom_module


class TrunkMediaAddressTest(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_address_survives_video_reoffer(self):
        received = []

        class Peer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, raw, addr):
                request = sip.parse_message(raw)
                received.append(request)
                if request.method not in {"INVITE", "BYE"}:
                    return
                headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                to = request.header("To")
                headers.append(("To", to if ";tag=" in to else to + ";tag=peer"))
                body = b""
                if request.method == "INVITE":
                    headers.extend((("Contact", f"<sip:peer@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>"),
                                    ("Content-Type", "application/sdp")))
                    selected = sdp.offered_pcm_formats(request.body)[0]
                    video = sdp.offered_video_formats(request.body)
                    body = sdp.build_answer_directional(
                        "127.0.0.1", "127.0.0.1", 45000, selected, selected,
                        remote_sdp=request.body, video_port=45002 if video else 0,
                        video_format=video[0] if video else None,
                        video_direction="sendonly",
                    ).encode()
                self.transport.sendto(sip.build_response(200, "OK", headers, body), addr)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        bind = _load_intercom_module("trunk_signaling").reuse_registered_trunk_flow
        try:
            for external in ("198.51.100.22", ""):
                client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="Browser", local_sip_port=0, local_rtp_port=41000,
                    local_video_rtp_port=41002, include_common_codecs=True,
                    video_formats=(sdp.DEFAULT_H264_FORMAT,), video_direction="recvonly")
                trunk = types.SimpleNamespace(config=types.SimpleNamespace(audio_codecs=None, video_codecs=None,
                    outbound_media_address=external))
                self.assertFalse(bind(trunk, client))  # Static/TCP calls also need the policy.
                expected = external or "127.0.0.1"
                try:
                    self.assertEqual(await client.invite(target="peer", remote_host="127.0.0.1", remote_sip_port=port,
                        request_uri=f"sip:peer@127.0.0.1:{port}", timeout=2), "in_call")
                    first = next(m for m in reversed(received) if m.method == "INVITE")
                    self.assertIn(f"c=IN IP4 {expected}\r\n".encode(), first.body)
                    self.assertIn("127.0.0.1", first.header("Contact"))
                    updated = await client.async_prepare_video_reinvite(local_video_rtp_port=41002,
                        video_formats=(sdp.DEFAULT_H264_FORMAT,), video_direction="sendrecv", timeout=2)
                    self.assertIsNotNone(updated)
                    latest = next(m for m in reversed(received) if m.method == "INVITE")
                    self.assertIn(f"c=IN IP4 {expected}\r\n".encode(), latest.body)
                    await client.terminate()
                finally:
                    await client.close()
                self.assertIsNone(client.transport)
        finally:
            transport.close()
