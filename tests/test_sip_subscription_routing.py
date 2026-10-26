"""Subscription requests follow dialog routing, not the subscriber's source port."""

from .voip_phase1_support import asyncio, audio_format, sip, sip_listener, unittest
from custom_components.voip_stack.core.sip_dialog import SubscriptionDialog


class SubscriptionRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_notify_uses_contact_or_proxy_on_a_different_port(self):
        for proxy in (False, True):
            with self.subTest(proxy=proxy):
                await self.exercise(proxy)

    async def exercise(self, proxy):
        received = asyncio.get_running_loop().create_future()
        writers, tasks = [], []

        async def next_hop(reader, writer):
            writers.append(writer)
            tasks.append(asyncio.current_task())
            try:
                raw = await sip_listener._read_sip_stream_message(reader)
                request = sip.parse_message(raw)
                headers = [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
                writer.write(sip.build_response(200, "OK", headers))
                await writer.drain()
                received.set_result(request)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        target = await asyncio.start_server(next_hop, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        contact_port = 9 if proxy else target_port

        async def handle(request, _address, _transport, _sender):
            state = SubscriptionDialog.from_request(request, "notifier")
            return sip_listener.SipRequestResult(to_tag="notifier", follow_up_dialog=state,
                follow_up=sip_listener.SipFollowUpRequest(method="NOTIFY", cseq=1,
                    headers=(("Event", "presence"), ("Subscription-State", "active;expires=60")),
                    content_type="application/pidf+xml", body=b"state"))

        server = sip_listener.SipTcpServer(host="127.0.0.1", port=0, local_ip="127.0.0.1",
            local_rtp_port=41000, supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None, on_request=handle)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        source_port = writer.get_extra_info("sockname")[1]
        try:
            headers = [("Via", f"SIP/2.0/TCP 127.0.0.1:{source_port};branch=z9hG4bKsubscription"),
                ("From", "<sip:watcher@127.0.0.1>;tag=watcher"), ("To", "<sip:HA@127.0.0.1>"),
                ("Contact", f"<sip:watcher@127.0.0.1:{contact_port};transport=tcp>"),
                ("Call-ID", "subscription-routing"), ("CSeq", "1 SUBSCRIBE"), ("Event", "presence")]
            if proxy:
                headers.append(("Record-Route", f"<sip:127.0.0.1:{target_port};transport=tcp;lr>"))
            writer.write(sip.build_request("SUBSCRIBE", "sip:HA@127.0.0.1", headers))
            await writer.drain()
            accepted = sip.parse_message(await asyncio.wait_for(sip_listener._read_sip_stream_message(reader), 1))
            self.assertEqual(accepted.status_code, 200)
            notification = await asyncio.wait_for(asyncio.shield(received), 1)
            self.assertEqual(notification.method, "NOTIFY")
            self.assertEqual(sip.parse_sip_uri(notification.uri).port, contact_port)
            self.assertEqual(bool(notification.header("Route")), proxy)
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            for opened in writers:
                opened.close()
            await asyncio.gather(*tasks, return_exceptions=True)
            target.close()
            await target.wait_closed()

    async def test_udp_notify_authenticates_and_follows_updated_contact(self):
        from .test_refer_correlation import Peer
        from custom_components.voip_stack.core.sip_auth import SipDigestCredentials, verify_digest_authorization
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        endpoint = sip_listener.SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)], on_invite=lambda _: None)
        local, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
        request = sip.parse_message(sip.build_request("SUBSCRIBE", "sip:desk@127.0.0.1", [
            ("From", "<sip:watcher@127.0.0.1>;tag=watcher"), ("To", "<sip:desk@127.0.0.1>"),
            ("Contact", f"<sip:watcher@127.0.0.1:{port}>"), ("Call-ID", "notify-auth"), ("CSeq", "1 SUBSCRIBE")]))
        state = SubscriptionDialog.from_request(request, "notifier")
        result = sip_listener.SipRequestResult(to_tag="notifier", follow_up_dialog=state,
            follow_up_credentials=SipDigestCredentials("watcher", "test-only"),
            follow_up=sip_listener.SipFollowUpRequest(method="NOTIFY", cseq=state.next_cseq(),
                headers=(("Event", "presence"), ("Subscription-State", "active;expires=60")), body=b"state"))
        task = asyncio.create_task(endpoint._send_application_follow_up(request, ("127.0.0.1", port), result))
        try:
            first, addr = await peer.receive("NOTIFY")
            headers = [(key, first.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
            headers.append(("WWW-Authenticate", 'Digest realm="watcher", nonce="challenge", qop="auth"'))
            transport.sendto(sip.build_response(401, "Unauthorized", headers), addr)
            retry, addr = await peer.receive("NOTIFY")
            self.assertEqual(sip.parse_cseq(retry.header("CSeq")).number, 2)
            self.assertNotEqual(retry.header("Via"), first.header("Via"))
            verify_digest_authorization(authorization_header=retry.header("Authorization"), username="watcher",
                password="test-only", method="NOTIFY", uri=retry.uri, realm="watcher", nonce="challenge")
            headers = [(key, retry.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
            headers.append(("Contact", "<sip:watcher@127.0.0.9:5090>"))
            transport.sendto(sip.build_response(200, "OK", headers), addr)
            self.assertEqual((await task).status_code, 200)
            self.assertEqual(state.remote_target_uri, "sip:watcher@127.0.0.9:5090")
            self.assertEqual(state.local_cseq, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            local.close()
            await endpoint.wait_closed()
            transport.close()
