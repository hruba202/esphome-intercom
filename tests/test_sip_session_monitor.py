"""Session lifetime observes renewed contracts without consuming SIP messages."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .voip_phase1_support import asyncio, sip, sip_client, sdp, unittest
from .test_refer_correlation import Peer


class SessionMonitorTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_after_remote_refresh_is_not_terminal(self):
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=41000)
        loop = asyncio.get_running_loop()
        timer = sip.SipSessionTimer()
        timer.configure(sip.SipSessionExpires(90, "uas"), local_role="uac", now=loop.time() - 60)
        client.dialog = SimpleNamespace(session_timer=timer)
        client._dialog_result = loop.create_future()
        terminate = AsyncMock()
        client._terminate_confirmed_dialog = terminate
        waits = 0

        async def wait(futures, *, timeout, return_when):
            nonlocal waits
            waits += 1
            if waits == 1:
                timer.configure(sip.SipSessionExpires(90, "uas"), local_role="uac", now=loop.time())
                return set(), set(futures)
            self.assertGreater(timeout, 59)
            client._dialog_result.set_result("remote_hangup")
            return {client._dialog_result}, set()

        with patch.object(sip_client.asyncio, "wait", side_effect=wait):
            result = await client._monitor_dialog()
        self.assertEqual(result, "remote_hangup")
        terminate.assert_not_awaited()
        self.assertEqual(waits, 2)

    async def test_remote_udp_update_rearms_lifetime_before_old_expiration(self):
        for initially_enabled in (False, True):
            with self.subTest(initially_enabled=initially_enabled):
                await self.exercise_remote_updates(initially_enabled)

    async def exercise_remote_updates(self, initially_enabled):
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
            local_rtp_port=41000, supported_send_rtp_formats=(fmt,), supported_recv_rtp_formats=(fmt,))
        watcher = None
        try:
            invite_task = asyncio.create_task(client.invite(target="peer", remote_host="127.0.0.1",
                remote_sip_port=transport.get_extra_info("sockname")[1], timeout=2))
            invite, addr = await peer.receive("INVITE")
            peer.answer(invite, addr, 200, b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n")
            self.assertEqual(await invite_task, "in_call")
            await peer.receive("ACK")
            if initially_enabled:
                client.dialog.session_timer.configure(sip.SipSessionExpires(90, "uas"), local_role="uac", now=loop.time() - 59.8)
            old_deadline = client.dialog.session_timer.expiration_notice_at or loop.time()
            watcher = asyncio.create_task(client.wait_for_dialog_termination())
            await asyncio.sleep(0)
            headers = [("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKrefresh"),
                ("From", invite.header("To") + ";tag=peer"), ("To", invite.header("From")),
                ("Call-ID", invite.header("Call-ID")), ("CSeq", "100 UPDATE"),
                ("Session-Expires", "90;refresher=uac"), ("Supported", "timer")]
            transport.sendto(sip.build_request("UPDATE", "sip:HA@127.0.0.1", headers), addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertGreater(client.dialog.session_timer.expiration_notice_at, old_deadline + 59)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(peer.receive("BYE"), .3)
            self.assertFalse(watcher.done())
            for sequence, timer_header, interval, local_refresher in (
                (101, "180;refresher=uas", 180, True), (102, "", 0, False),
                (103, "90;refresher=uac", 90, False),
            ):
                update = [(key, f"{sequence} UPDATE" if key == "CSeq" else
                           f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKrefresh{sequence}" if key == "Via" else value)
                          for key, value in headers if key != "Session-Expires"]
                if timer_header:
                    update.append(("Session-Expires", timer_header))
                transport.sendto(sip.build_request("UPDATE", "sip:HA@127.0.0.1", update), addr)
                self.assertEqual((await peer.receive())[0].status_code, 200)
                self.assertEqual(client.dialog.session_timer.interval, interval)
                self.assertEqual(client.dialog.session_timer.local_refresher, local_refresher)
                self.assertFalse(watcher.done())
            headers = [(key, "104 BYE" if key == "CSeq" else value) for key, value in headers
                       if key not in {"Session-Expires", "Supported"}]
            transport.sendto(sip.build_request("BYE", "sip:HA@127.0.0.1", headers), addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertEqual(await asyncio.wait_for(watcher, 1), "remote_hangup")
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await client.close()
            transport.close()

    async def test_new_timer_wakes_disabled_monitor_and_true_expiry_terminates(self):
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=41000)
        loop = asyncio.get_running_loop()
        client.dialog = SimpleNamespace(session_timer=sip.SipSessionTimer())
        client._dialog_result = loop.create_future()
        client._terminate_confirmed_dialog = AsyncMock()
        monitor = asyncio.create_task(client._monitor_dialog())
        try:
            await asyncio.sleep(0)
            timer = sip.SipSessionTimer()
            timer.configure(sip.SipSessionExpires(90, "uas"), local_role="uac", now=loop.time() - 61)
            client.dialog = SimpleNamespace(session_timer=timer)
            self.assertEqual(await asyncio.wait_for(monitor, 1), "session_timer_expired")
            client._terminate_confirmed_dialog.assert_awaited_once()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            client.dialog = None
