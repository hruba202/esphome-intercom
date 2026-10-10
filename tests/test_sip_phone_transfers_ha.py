"""Independent registered SIP phones exercise consultation and transfers."""

import asyncio
import math
import shutil
import struct
import wave

import pytest

from custom_components.voip_stack.endpoint_routing import roster_from_peers
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
from tools.ha_softphone_matrix import BareSip
from .test_trunk_calls_ha import trunks, lab

pytestmark = [pytest.mark.ha, pytest.mark.skipif(shutil.which("baresip") is None, reason="baresip is not installed")]


@pytest.fixture
async def registered_phones(trunks, tmp_path):
    lab = trunks
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    names = ("transfer_a", "transfer_b", "transfer_c")
    registrar.update_accounts([SipAccount(name, name, "isolated-test-only") for name in names])
    for name in names:
        lab.runtime.endpoints.register(PhoneEndpoint(
            endpoint_id=name, name=name, username=name, kind=EndpointKind.SIP_ACCOUNT,
        ))
    peers = []
    try:
        for index, name in enumerate(names):
            config = tmp_path / name
            config.mkdir(mode=0o700)
            (config / "config").write_text(f"""net_interface 127.0.0.1
sip_listen 127.0.0.1:0
call_max_calls 2
audio_player aubridge,nil
audio_source ausine,{450 + index * 100}
audio_alert aubridge,nil
snd_path {config}
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
            accounts = config / "accounts"
            accounts.write_text(
                f'<sip:{name}@127.0.0.1:{lab.runtime.transport_config["sip_port"]}>'
                f';auth_user={name};auth_pass=isolated-test-only;regint=60;answermode=auto\n'
            )
            accounts.chmod(0o600)
            peers.append(await asyncio.to_thread(BareSip, config))
        lab.roster[:] = roster_from_peers(lab.hass, [], registrar.registered_roster_entries())
        yield peers
    finally:
        for peer in peers:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.hangup)
        try:
            async with asyncio.timeout(3):
                while lab.calls.sessions:
                    for peer in peers:
                        peer.read()
                    await asyncio.sleep(0.01)
        except TimeoutError:
            import io
            from pathlib import Path

            trace = io.StringIO()
            for task in asyncio.all_tasks():
                task.print_stack(file=trace)
            Path("test_captures/ha-pbx/transfer-cleanup-stacks.txt").write_text(trace.getvalue())
            raise
        finally:
            for peer in peers:
                await asyncio.to_thread(peer.close)
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.relays_snapshot()


async def establish_consultation(lab, peers):
    caller, operator, _destination = peers
    await asyncio.to_thread(caller.dial, "transfer_b")
    await asyncio.to_thread(operator.wait_for, "Call established", 5)
    original = lab.runtime.endpoints.get("transfer_b").active_call_id
    assert original
    start = len(operator.read())
    target = f'sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
    await asyncio.to_thread(operator.command, f"/atransferstart {target}")
    async with asyncio.timeout(3):
        while "Call established" not in operator.read()[start:]:
            assert "486 Busy Here" not in operator.output[start:], operator.output[start:]
            await asyncio.sleep(0.01)
    return original, next(item for item in lab.calls.sessions.values() if item.call_id != original)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_phone_consultation_preserves_original_call(registered_phones, trunks):
    lab = trunks
    caller, operator, destination = registered_phones
    original, _consultation = await establish_consultation(lab, registered_phones)
    assert len(lab.calls.sessions) == 2
    assert sum("transfer_b" in session.endpoint_claims for session in lab.calls.sessions.values()) == 2
    assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    await asyncio.to_thread(destination.hangup)
    async with asyncio.timeout(3):
        while len(lab.calls.sessions) != 1:
            operator.read()
            await asyncio.sleep(0.01)
    assert lab.calls.get_session(original).live
    assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    assert not lab.runtime.endpoints.get("transfer_c").active_call_id
    await asyncio.to_thread(caller.hangup)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("hangup_side,redial", [(0, False), (2, False), (2, True)])
async def test_attended_transfer_reuses_destination_and_delivers_duplex_audio(registered_phones, trunks, tmp_path, hangup_side, redial):
    lab = trunks
    caller, operator, destination = registered_phones
    original, consultation = await establish_consultation(lab, registered_phones)
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    _source, wire_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(wire_id)
    await asyncio.to_thread(operator.command, "/atransferexec")
    async with asyncio.timeout(4):
        while True:
            for peer in registered_phones:
                peer.read()
            sessions = tuple(lab.calls.sessions.values())
            if (len(sessions) == 1 and sessions[0].call_id not in {original, consultation.call_id}
                    and not lab.runtime.endpoints.get("transfer_b").active_call_id):
                break
            assert "transfer failed" not in operator.output.lower(), operator.output
            await asyncio.sleep(0.01)
    replacement = sessions[0]
    assert lab.calls.resource_for(replacement.call_id, "relay") is relay
    assert lab.calls.sip_client_for(wire_id) is client
    assert lab.runtime.endpoints.get("transfer_a").active_call_id == replacement.call_id
    assert lab.runtime.endpoints.get("transfer_c").active_call_id == replacement.call_id
    assert destination.output.count("Call established") == 1
    assert not list(lab.calls.resource_items("replacement_grant"))
    left, right = relay.left_tx_packets, relay.right_tx_packets
    async with asyncio.timeout(4):
        while relay.left_tx_packets < left + 30 or relay.right_tx_packets < right + 30:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(0.01)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(0.01)
    if redial:
        offset = len(caller.read())
        await asyncio.to_thread(caller.command, "/dial transfer_c")
        async with asyncio.timeout(3):
            while "Call established" not in caller.read()[offset:]:
                await asyncio.sleep(0.01)
        assert len(lab.calls.sessions) == 1
        current = next(iter(lab.calls.sessions.values()))
        assert set(current.endpoint_claims) == {"transfer_a", "transfer_c"}
        next_relay = lab.calls.resource_for(current.call_id, "relay")
        async with asyncio.timeout(3):
            while next_relay.left_tx_packets < 30 or next_relay.right_tx_packets < 30:
                await asyncio.sleep(0.01)
        await asyncio.to_thread(destination.hangup)
        await asyncio.wait_for(current.terminated.wait(), 3)
    # sndfile finalizes WAV headers when the independent peer releases audio.
    # SIP idle alone does not guarantee that its audio filter has flushed yet.
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 650), ("transfer_c", 450)):
        found = False
        for path in (tmp_path / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as audio:
                assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
                rate = audio.getframerate()
                samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))[-1600:]
            if len(samples) < 800:
                continue
            powers = {frequency: abs(sum(value * complex(
                math.cos(2 * math.pi * frequency * index / rate),
                math.sin(2 * math.pi * frequency * index / rate),
            ) for index, value in enumerate(samples))) for frequency in (450, 550, 650)}
            found |= max(powers, key=powers.get) == expected and powers[expected] > 10000
        assert found, f"{name} did not receive the other phone's recorded tone"


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_replacement_authorization_races_and_rollback_preserve_existing_calls(registered_phones, trunks):
    from dataclasses import replace
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    invite = replace(dialog.invite, call_id="replacement-attempt", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a")
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    previous_peer = relay.left
    mismatch = await prepare_replacement(lab.hass, replace(invite, replaces=replace(key, to_tag="wrong")), "127.0.0.1")
    assert mismatch.status == 481
    early = await prepare_replacement(lab.hass, replace(invite, replaces=replace(key, early_only=True)), "127.0.0.1")
    assert early.status == 486
    uninvited = await prepare_replacement(lab.hass, invite, "127.0.0.1")
    assert uninvited.status == 403
    assert len(lab.calls.sessions) == 2
    _root, referring_id = lab.calls.bridge_for(original)
    referring_client = lab.calls.sip_client_for(referring_id)
    target = SipReferTarget(f'sip:transfer_c@127.0.0.1:{manager.port}', key)
    grant = prepare_replacement_grant(lab.runtime, original, target, referring_client)
    try:
        _source, occupied_id = lab.calls.bridge_for(consultation.call_id)
        collision = await prepare_replacement(lab.hass, replace(invite, call_id=occupied_id), "127.0.0.1")
        assert collision.status == 482
        assert consultation.live and lab.calls.sip_client_for(occupied_id).dialog is not None
        prepared = await prepare_replacement(lab.hass, invite, "127.0.0.1")
        assert prepared.status == 200
        duplicate = await prepare_replacement(lab.hass, replace(invite, call_id="second-attempt"), "127.0.0.1")
        assert duplicate.status == 491
        assert len(lab.calls.sessions) == 3
        await prepared.rollback()
        assert len(lab.calls.sessions) == 2
        assert relay.left is previous_peer
        assert consultation.live and lab.calls.get_session(original).live
        assert not list(lab.calls.resource_items("replacement_grant"))
    finally:
        grant.release()
    grant = prepare_replacement_grant(lab.runtime, original, target, referring_client)
    try:
        prepared = await prepare_replacement(lab.hass, replace(invite, call_id="late-replacement"), "127.0.0.1")
        assert prepared.status == 200
        await asyncio.to_thread(registered_phones[2].hangup)
        await asyncio.wait_for(consultation.terminated.wait(), 3)
        with pytest.raises(RuntimeError, match="ownership changed"):
            await prepared.commit()
        await prepared.rollback()
        assert tuple(lab.calls.sessions) == (original,)
        assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    finally:
        grant.release()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_phone_cannot_replace_another_call_without_referral(registered_phones, trunks):
    from dataclasses import replace
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    caller = registered_phones[0]
    for reference, expected in ((key, "403 Forbidden"), (replace(key, to_tag="wrong"), "481 Call")):
        offset = len(caller.read())
        target = SipReferTarget(f"sip:transfer_c@127.0.0.1:{manager.port}", reference)
        await asyncio.to_thread(caller.command, f"/dial {target.as_header()}")
        async with asyncio.timeout(3):
            while expected not in caller.read()[offset:]:
                await asyncio.sleep(0.01)
        assert set(lab.calls.sessions) == {original, consultation.call_id}
        assert consultation.live
        assert lab.runtime.endpoints.get("transfer_c").active_call_id == consultation.call_id

