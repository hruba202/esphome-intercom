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
