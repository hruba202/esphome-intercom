#!/usr/bin/env python3
"""Qualify an isolated Asterisk transfer with real baresip peers and decoded tones.

Requires Asterisk, baresip (g711, ausine, aubridge and sndfile modules), and
FFmpeg. All SIP sockets bind to loopback. The PBX and peer processes are owned
by this run and are stopped before a successful result is written.
"""

import argparse
import asyncio
import json
import logging
import math
import secrets
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.voip_phase1_support import sip_client, sdp, sip_transfer  # noqa: E402
from tools.ha_softphone_matrix import BareSip  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--authenticate",
    action="store_true",
    help="Require Digest authentication on incoming INVITEs",
)
parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
parser.add_argument("--mode", choices=("blind", "attended"), default="blind")
parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
parser.add_argument("--asterisk-data", type=Path, default=Path("/var/lib/asterisk"))
args = parser.parse_args()
BASE = args.output.resolve()
BASE.mkdir(parents=True, exist_ok=False)
logging.basicConfig(level=logging.INFO)


def port(kind=socket.SOCK_DGRAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main():
    sip_port = port(
        socket.SOCK_STREAM if args.transport == "tcp" else socket.SOCK_DGRAM
    )
    outgoing_port, b_port, c_port = port(), port(), port()
    password = secrets.token_urlsafe(24) if args.authenticate else ""
    for name in ("etc", "run", "db", "log", "spool", "cache"):
        (BASE / name).mkdir(exist_ok=True)
    config = BASE / "etc" / "asterisk.conf"
    config.write_text(
        "[directories]\n"
        + "\n".join(
            f"{key} => {value}"
            for key, value in {
                "astetcdir": BASE / "etc",
                "astmoddir": "/usr/lib/asterisk/modules",
                "astvarlibdir": args.asterisk_data.resolve(),
                "astdbdir": BASE / "db",
                "astkeydir": BASE / "db",
                "astdatadir": args.asterisk_data.resolve(),
                "astagidir": BASE / "etc",
                "astspooldir": BASE / "spool",
                "astrundir": BASE / "run",
                "astlogdir": BASE / "log",
                "astcachedir": BASE / "cache",
            }.items()
        )
        + "\n[options]\nverbose=3\n"
    )
    modules = (
        "res_pjproject res_sorcery_config res_sorcery_memory res_sorcery_astdb "
        "res_pjsip res_pjsip_session res_pjsip_sdp_rtp res_pjsip_pubsub res_pjsip_refer "
        "res_pjsip_endpoint_identifier_ip res_pjsip_endpoint_identifier_user "
        "res_pjsip_authenticator_digest res_pjsip_outbound_authenticator_digest "
        "res_pjsip_caller_id res_pjsip_logger chan_pjsip res_rtp_asterisk codec_ulaw codec_alaw "
        "bridge_simple bridge_native_rtp pbx_config app_dial app_transfer app_stack func_channel"
    )
    (BASE / "etc" / "modules.conf").write_text(
        "[modules]\nautoload=no\n"
        + "".join(f"load={name}.so\n" for name in modules.split())
    )
    (BASE / "etc" / "pjsip.conf").write_text(f"""[transport]
type=transport
protocol={args.transport}
bind=127.0.0.1:{sip_port}
[transport-outbound]
type=transport
protocol=udp
bind=127.0.0.1:{outgoing_port}
[phone]
type=endpoint
context=lab
disallow=all
allow=ulaw
direct_media=no
allow_transfer=yes
{("auth=ha-auth" if args.authenticate else "")}
[local]
type=identify
endpoint=phone
match=127.0.0.1
""")
    if args.authenticate:
        authentication = BASE / "etc" / "pjsip.conf"
        authentication.chmod(0o600)
        with authentication.open("a") as stream:
            stream.write(
                f"[ha-auth]\ntype=auth\nauth_type=userpass\nusername=ha\npassword={password}\n"
            )
    (BASE / "etc" / "extensions.conf").write_text(f"""[lab]
exten => 1001,1,Dial(PJSIP/phone/sip:b@127.0.0.1:{b_port})
exten => 1002,1,Dial(PJSIP/phone/sip:c@127.0.0.1:{c_port})
""")
    (BASE / "etc" / "rtp.conf").write_text("[general]\nrtpstart=18000\nrtpend=19000\n")
    (BASE / "etc" / "logger.conf").write_text(
        "[logfiles]\nconsole=notice,warning,error,verbose\n"
    )
    previous_wavs = set(BASE.glob("*/*-dec.wav"))
    peers = []
    pbx = None
    client = None
    watcher = None
    consultation = consultation_watcher = consultation_socket = None
    log = (BASE / "asterisk.log").open("wb")
    audio_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_socket.bind(("127.0.0.1", 0))
    audio_socket.setblocking(False)
    try:
        for name, listen, tone in [("b", b_port, 450), ("c", c_port, 650)]:
            directory = BASE / name
            directory.mkdir(exist_ok=True)
            (directory / "config").write_text(f"""net_interface 127.0.0.1
sip_listen 127.0.0.1:{listen}
rtp_ports 20000-30000
audio_player aubridge,nil
audio_source ausine,{tone}
audio_alert aubridge,nil
snd_path {directory}
module_path /usr/lib/baresip/modules
module stdio.so
module menu.so
module g711.so
module auconv.so
module auresamp.so
module ausine.so
module aubridge.so
module sndfile.so
module_app account.so
""")
            (directory / "accounts").write_text(
                f"<sip:{name}@127.0.0.1:{sip_port}>;regint=0;answermode=auto\n"
            )
            peers.append(
                await asyncio.to_thread(BareSip, directory, wait_for_registration=False)
            )
        pbx = subprocess.Popen(
            ["asterisk", "-f", "-q", "-C", str(config)], stdout=log, stderr=log
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            check = await asyncio.to_thread(
                subprocess.run,
                ["asterisk", "-C", str(config), "-rx", "pjsip show endpoints"],
                capture_output=True,
                text=True,
            )
            if "phone" in check.stdout:
                break
            assert pbx.poll() is None, "Asterisk stopped during startup"
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Asterisk endpoint not ready")
        subprocess.run(["asterisk", "-C", str(config), "-rx", "pjsip set logger on"], stdout=log, stderr=log, check=True)
        pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            signaling_transport=args.transport.upper(),
            username="ha",
            auth_username="ha",
            password=password,
            local_sip_port=0,
            local_rtp_port=audio_socket.getsockname()[1],
            supported_send_rtp_formats=(pcmu,),
            supported_recv_rtp_formats=(pcmu,),
        )
        outcome = await client.invite(
            target="1001", remote_host="127.0.0.1", remote_sip_port=sip_port, timeout=5
        )
        if outcome == "ringing":
            outcome = await client.wait_for_final(timeout=5)
        assert outcome == "in_call", outcome
        watcher = asyncio.create_task(client.wait_for_dialog_termination())
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=550:sample_rate=8000:duration=0.6",
                "-f",
                "mulaw",
                str(BASE / "send.ulaw"),
            ],
            check=True,
        )
        encoded = (BASE / "send.ulaw").read_bytes()

        async def send_audio(owner=client, source=audio_socket):
            loop = asyncio.get_running_loop()
            started = loop.time()
            for index in range(len(encoded) // 160):
                raw = (
                    struct.pack("!BBHII", 0x80, 0, index, index * 160, 1234567)
                    + encoded[index * 160 : (index + 1) * 160]
                )
                await loop.sock_sendto(
                    source,
                    raw,
                    (owner.dialog.remote_rtp_host, owner.dialog.remote_rtp_port),
                )
                await asyncio.sleep(max(0, started + (index + 1) * 0.02 - loop.time()))

        transmitting = asyncio.create_task(send_audio())
        payloads = []
        async with asyncio.timeout(3):
            while len(payloads) < 30:
                raw = await asyncio.get_running_loop().sock_recv(audio_socket, 2048)
                from custom_components.voip_stack.core import rtp

                packet = rtp.parse_packet(raw)
                assert packet.payload_type == 0
                payloads.append(packet.payload)
        await transmitting
        (BASE / "received.ulaw").write_bytes(b"".join(payloads))
        replaces = None
        if args.mode == "attended":
            consultation_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            consultation_socket.bind(("127.0.0.1", 0))
            consultation_socket.setblocking(False)
            consultation = sip_client.SipCallClient(
                local_ip="127.0.0.1",
                local_name="HA-consultation",
                signaling_transport=args.transport.upper(),
                username="ha",
                auth_username="ha",
                password=password,
                local_sip_port=0,
                local_rtp_port=consultation_socket.getsockname()[1],
                supported_send_rtp_formats=(pcmu,),
                supported_recv_rtp_formats=(pcmu,),
            )
            outcome = await consultation.invite(
                target="1002",
                remote_host="127.0.0.1",
                remote_sip_port=sip_port,
                timeout=5,
            )
            if outcome == "ringing":
                outcome = await consultation.wait_for_final(timeout=5)
            assert outcome == "in_call", outcome
            consultation_watcher = asyncio.create_task(
                consultation.wait_for_dialog_termination()
            )
            await send_audio(consultation, consultation_socket)
            replaces = sip_transfer.SipReplaces(
                consultation.dialog_ids.call_id,
                consultation.dialog_ids.remote_tag,
                consultation.dialog_ids.local_tag,
            )
        transfer = await client.refer(
            sip_transfer.SipReferTarget(f"sip:1002@127.0.0.1:{sip_port}", replaces),
            timeout=10,
        )
        assert transfer.accepted, transfer
        await client.terminate()
        if consultation is not None:
            await consultation.terminate()
            await asyncio.wait_for(consultation_watcher, 3)
        await asyncio.to_thread(peers[1].wait_for, "Call established", 5)
        await asyncio.sleep(0.6)
        for peer in peers:
            peer.read()
        await asyncio.to_thread(peers[1].hangup)
        await asyncio.wait_for(watcher, 3)
        await client.close()
        await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "mulaw",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-i",
                str(BASE / "received.ulaw"),
                str(BASE / "received.wav"),
            ],
            check=True,
        )
        with wave.open(str(BASE / "received.wav"), "rb") as stream:
            samples = struct.unpack(
                f"<{stream.getnframes()}h", stream.readframes(stream.getnframes())
            )
        power = {
            freq: abs(
                sum(
                    value
                    * complex(
                        math.cos(2 * math.pi * freq * i / 8000),
                        math.sin(2 * math.pi * freq * i / 8000),
                    )
                    for i, value in enumerate(samples)
                )
            )
            for freq in (450, 550, 650)
        }
        assert max(power, key=power.get) == 450 and power[450] > 10000
        for peer in peers:
            await asyncio.to_thread(peer.close)
        decoded = {}
        for name, expected in [
            ("b", (550, 650)),
            ("c", (450, 550) if consultation is not None else (450,)),
        ]:
            detected = set()
            for filename in set((BASE / name).glob("*-dec.wav")) - previous_wavs:
                with wave.open(str(filename), "rb") as recording:
                    rate = recording.getframerate()
                    values = struct.unpack(
                        f"<{recording.getnframes()}h",
                        recording.readframes(recording.getnframes()),
                    )
                for start in range(0, len(values) - 1600, 800):
                    samples_window = values[start : start + 1600]
                    strengths = {
                        freq: abs(
                            sum(
                                value
                                * complex(
                                    math.cos(2 * math.pi * freq * i / rate),
                                    math.sin(2 * math.pi * freq * i / rate),
                                )
                                for i, value in enumerate(samples_window)
                            )
                        )
                        for freq in (450, 550, 650)
                    }
                    winner = max(strengths, key=strengths.get)
                    if strengths[winner] > 10000:
                        detected.add(winner)
            assert set(expected) <= detected, (name, detected)
            decoded[name] = sorted(detected)
        result = {
            "passed": True,
            "mode": args.mode,
            "digest_authentication": args.authenticate,
            "client_signaling_transport": args.transport.upper(),
            "asterisk": subprocess.check_output(["asterisk", "-V"], text=True).strip(),
            "refer_status": transfer.status,
            "received_audio_codec": "PCMU/8000",
            "received_tone_hz": 450,
            "rtp_packets": len(payloads),
            "independent_decoded_peer_tones": decoded,
        }
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if consultation_watcher is not None:
            consultation_watcher.cancel()
            await asyncio.gather(consultation_watcher, return_exceptions=True)
        if consultation is not None:
            await consultation.close()
        if consultation_socket is not None:
            consultation_socket.close()
        if client is not None:
            await client.close()
        audio_socket.close()
        for peer in peers:
            (BASE / f"{peers.index(peer)}-baresip.log").write_text(
                peer.output if peer.proc.poll() is not None else peer.read()
            )
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.close)
        if pbx is not None:
            subprocess.run(
                ["asterisk", "-C", str(config), "-rx", "core stop now"],
                stdout=log,
                stderr=log,
                timeout=5,
            )
            try:
                pbx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pbx.terminate()
                pbx.wait(timeout=5)
        log.close()
        if password:
            authentication = BASE / "etc" / "pjsip.conf"
            authentication.write_text(
                authentication.read_text().replace(password, "REGENERATED_FOR_EACH_RUN")
            )
    return result


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
    except BaseException as error:
        (BASE / "result.json").write_text(
            json.dumps({"passed": False, "error": str(error)}, indent=2)
        )
        raise
    (BASE / "result.json").write_text(json.dumps(result, indent=2))
