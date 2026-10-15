"""REFER subscription identity through both dialog owners on UDP loopback."""

from .voip_phase1_support import asyncio, audio_format, sdp, sip, sip_client, sip_listener, sip_transfer, unittest


class Peer(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.messages.put_nowait((sip.parse_message(data), addr))

    async def receive(self, method=None):
        async with asyncio.timeout(2):
            while True:
                message, addr = await self.messages.get()
                if method is None or message.method == method:
                    return message, addr

    def answer(self, request, addr, status=202, body=b""):
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        to = request.header("To")
        headers.append(("To", to if ";tag=" in to else to + ";tag=peer"))
        headers.append(("Contact", f'<sip:peer@127.0.0.1:{self.transport.get_extra_info("sockname")[1]}>'))
        if body:
            headers.append(("Content-Type", "application/sdp"))
        self.transport.sendto(sip.build_response(status, "Result", headers, body), addr)

    def notify(self, refer, addr, cseq, event, status=200, method="NOTIFY"):
        raw = sip.build_request(method, "sip:HA@127.0.0.1", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKnotify{cseq}"),
            ("From", refer.header("To")), ("To", refer.header("From")),
            ("Call-ID", refer.header("Call-ID")), ("CSeq", f"{cseq} {method}"),
            ("Event", event), ("Content-Type", "message/sipfrag"),
            ("Subscription-State", "terminated;reason=noresource" if status >= 200 else "active;expires=30"),
        ], f"SIP/2.0 {status} Result\r\n".encode())
        self.transport.sendto(raw, addr)
        return raw


class ReferCorrelationTest(unittest.IsolatedAsyncioTestCase):
    async def test_uac_and_uas_consecutive_notify_transactions(self):
        for direction in ("uac", "uas"):
            with self.subTest(direction=direction):
                await self.exercise(direction)

    async def exercise(self, direction):
        loop = asyncio.get_running_loop()
        peer_transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        peer_port = peer_transport.get_extra_info("sockname")[1]
        audio = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        owner_transport = None
        owner = None
        task = None
        try:
            if direction == "uac":
                owner = sip_client.SipCallClient(
                    local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=41000,
                    supported_send_rtp_formats=(audio,), supported_recv_rtp_formats=(audio,),
                )
                task = asyncio.create_task(owner.invite(target="peer", remote_host="127.0.0.1",
                                                       remote_sip_port=peer_port, timeout=2))
                invite, addr = await peer.receive("INVITE")
                peer.answer(invite, addr, 200, (
                    b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    b"m=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
                ))
                self.assertEqual(await task, "in_call")
                async def refer(timeout=1):
                    return await owner.refer(sip_transfer.SipReferTarget("sip:next@127.0.0.1"), timeout=timeout)
            else:
                owner = sip_listener.SipUdpEndpoint(
                    local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
                    supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
                    on_invite=lambda _invite: None,
                )
                owner_transport, _ = await loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
                invite = sip.parse_message(sip.build_request("INVITE", "sip:HA@127.0.0.1", [
                    ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", "<sip:HA@127.0.0.1>"),
                    ("Call-ID", "uas-correlation"), ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:peer@127.0.0.1:{peer_port}>"),
                ]))
                owner.active_dialogs["uas-correlation"] = sip_listener._ActiveDialog(
                    request=invite, addr=("127.0.0.1", peer_port), to_tag="ha", cseq=2, transport="UDP",
                    remote_uri=f"sip:peer@127.0.0.1:{peer_port}",
                )
                async def refer(timeout=1):
                    return await owner.async_refer("uas-correlation", sip_transfer.SipReferTarget("sip:next@127.0.0.1"), timeout=timeout)

            task = asyncio.create_task(refer())
            first, addr = await peer.receive("REFER")
            # A NOTIFY may arrive before the REFER's final response.
            peer.notify(first, addr, 10, "refer", 100)
            response, _ = await peer.receive()
            if response.method == "ACK":
                response, _ = await peer.receive()
            self.assertEqual(response.status_code, 200)
            peer.answer(first, addr)
            first_id = sip.parse_cseq(first.header("CSeq")).number
            duplicate = peer.notify(first, addr, 11, f"refer;id={first_id}", 486)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertEqual((await task).status, 486)

            task = asyncio.create_task(refer())
            second, addr = await peer.receive("REFER")
            second_id = sip.parse_cseq(second.header("CSeq")).number
            self.assertNotEqual(first_id, second_id)
            peer.answer(second, addr)
            peer_transport.sendto(duplicate, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            for seq, event in ((12, f"refer;id={first_id}"), (13, "refer;id=999999"), (14, "refer")):
                peer.notify(second, addr, seq, event)
                self.assertEqual((await peer.receive())[0].status_code, 481)
                self.assertFalse(task.done())
            progress = peer.notify(second, addr, 20, f"refer;id={second_id}", 100)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            peer_transport.sendto(progress, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            peer.notify(second, addr, 19, f"refer;id={second_id}")
            self.assertEqual((await peer.receive())[0].status_code, 500)
            self.assertFalse(task.done())
            peer.notify(second, addr, 21, f"refer;id={second_id}")
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertTrue((await task).accepted)

            task = asyncio.create_task(refer(timeout=0.05))
            third, addr = await peer.receive("REFER")
            peer.answer(third, addr)
            self.assertEqual((await task).state, "timeout")

            task = asyncio.create_task(refer())
            fourth, addr = await peer.receive("REFER")
            peer.answer(fourth, addr)
            peer.notify(fourth, addr, 30, "refer", method="BYE")
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertFalse((await task).accepted)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if direction == "uac" and owner is not None:
                await owner.close()
            if owner_transport is not None:
                owner_transport.close()
            peer_transport.close()
