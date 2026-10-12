"""Directed pickup retains the caller while a different real phone answers."""

import asyncio
import math
import struct
import wave

import pytest
from custom_components.voip_stack.store import config_entry
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("target", ["pickup.transfer_b", "*8101"])
@pytest.mark.parametrize("hangup_side", [0, 2])
async def test_directed_sip_pickup_has_duplex_audio_and_one_caller(
    trunks, registered_phones, tmp_path, target, hangup_side
):
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    runtime.runtime.endpoints.update("transfer_b", extension="101")
    caller, old_phone, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    original = next(iter(runtime.calls.sessions.values()))
    await asyncio.to_thread(picker.dial, target)
    await wait_for(
        lambda: original.state == "in_call" and "Call established" in caller.read()
    )
    assert len(runtime.calls.sessions) == 1
    assert caller.read().count("Call established") == 1
    assert "Call established" not in old_phone.read()
    assert (
        runtime.runtime.endpoints.get("transfer_c").active_call_id == original.call_id
    )
    await wait_for(
        lambda: not runtime.runtime.endpoints.get("transfer_b").active_call_id
    )
    relay = runtime.calls.resource_for(original.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, tone in (("transfer_a", 650), ("transfer_c", 450)):
        found = False
        for path in (tmp_path / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                samples = struct.unpack(
                    f"<{wav.getnframes()}h", wav.readframes(wav.getnframes())
                )[-1600:]
            if len(samples) < 800:
                continue
            power = {
                f: abs(
                    sum(
                        value
                        * complex(
                            math.cos(2 * math.pi * f * i / rate),
                            math.sin(2 * math.pi * f * i / rate),
                        )
                        for i, value in enumerate(samples)
                    )
                )
                for f in (450, 650)
            }
            found |= power[tone] > power[1100 - tone] * 5 and power[tone] > 10000
        assert found, f"{name} did not receive the opposite microphone"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize(
    "winner", ["original_answer", "caller_hangup", "picker_cancel"]
)
async def test_pickup_preparation_cannot_steal_answered_or_cancelled_call(
    trunks, registered_phones, monkeypatch, winner
):
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    caller, original, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    session = next(iter(runtime.calls.sessions.values()))
    entered, release = asyncio.Event(), asyncio.Event()
    start = SipRtpRelay.start
    first = True

    async def gated(relay):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await start(relay)

    monkeypatch.setattr(SipRtpRelay, "start", gated)
    try:
        await asyncio.to_thread(picker.command, "/dial pickup.transfer_b")
        await asyncio.wait_for(entered.wait(), 3)
        if winner == "original_answer":
            await asyncio.to_thread(original.command, "/accept")
            await wait_for(lambda: session.state == "in_call")
        elif winner == "caller_hangup":
            await asyncio.to_thread(caller.hangup)
        else:
            from custom_components.voip_stack.runtime_data import sip_endpoint_manager

            manager = sip_endpoint_manager(runtime.hass)
            pending = manager.udp_server.endpoint.pending_invites
            picker_id = next(key for key in pending if key != session.call_id)
            await asyncio.to_thread(picker.hangup)
            await wait_for(lambda: picker_id not in pending)
        release.set()
        await wait_for(
            lambda: not runtime.runtime.endpoints.get("transfer_c").active_call_id
        )
        assert "Call established" not in picker.read()
        if winner == "picker_cancel":
            assert session.live and session.state == "remote_ringing"
            await asyncio.to_thread(original.command, "/accept")
            await wait_for(lambda: session.state == "in_call")
        if winner != "caller_hangup":
            assert session.live
            await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        release.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_browser_picks_up_local_phone_using_original_session(trunks, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    for name in ("local-a", "local-b", "local-c"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                name=name,
                device_id="device-" + name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        runtime.roster.append(
            RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name})
        )
    runtime.hass.states.async_set(
        "sensor.voip_phonebook",
        "ready",
        {"roster_json": dump_roster_json(runtime.roster)},
    )
    monkeypatch.setattr(
        softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1")
    )
    monkeypatch.setattr(
        softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
    )
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass, "local-a", "local-b", caller_owner_id="owner-a"
    )
    session = runtime.calls.get_session(call.call_id)
    await runtime.hass.services.async_call(
        "voip_stack",
        "call",
        {
            "device_id": "device-local-c",
            "destination": "pickup.local-b",
            "media_client_id": "owner-c",
        },
        blocking=True,
    )
    assert runtime.calls.get_session(call.call_id) is session
    assert not runtime.runtime.endpoints.get("local-b").active_call_id
    assert runtime.runtime.endpoints.get("local-c").active_call_id == call.call_id
    caller = bridge.acquire_media(call.call_id, "local-a", "owner-a")
    picker = bridge.acquire_media(call.call_id, "local-c", "owner-c")
    for source, lease, dest, other, sample in [
        ("local-a", caller, "local-c", picker, 7000),
        ("local-c", picker, "local-a", caller, 9000),
    ]:
        pcm = struct.pack("<480h", *([sample] * 480))
        bridge.send_audio(call.call_id, source, lease.token, pcm)
        assert bridge.receive_audio_nowait(call.call_id, dest, other.token) == pcm
    await runtime.hass.services.async_call(
        "voip_stack", "hangup", {"device_id": "device-local-c"}, blocking=True
    )
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_phone_picks_up_local_browser_call(
    trunks, registered_phones, monkeypatch
):
    from aiohttp import WSMsgType
    from types import SimpleNamespace
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    for name in ("local-a", "local-b"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                name=name,
                device_id="device-" + name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
    async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass, "local-a", "local-b", caller_owner_id="owner-a"
    )
    await asyncio.to_thread(registered_phones[2].dial, "pickup.local-b")
    session = runtime.calls.get_session(call.call_id)
    assert session.state == "in_call"
    assert not runtime.runtime.endpoints.get("local-b").active_call_id
    media = audio_ws_view._active_softphone_media_session(runtime.hass, "local-a")
    assert media is not None

    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.audio = asyncio.Queue()
            self.ready = asyncio.Event()

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

        async def send_json(self, value):
            self.ready.set()

        async def send_bytes(self, value):
            await self.audio.put(value)

    browser = Browser()
    worker = asyncio.create_task(
        audio_ws_view._run_audio_session(
            runtime.hass, browser, media, endpoint_id="local-a"
        )
    )
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        frame = (
            struct.pack("<h", 7000)
            * media.send_format.audio_format.nominal_frame_samples
        )
        for _ in range(20):
            await browser.messages.put(
                SimpleNamespace(
                    type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)
                )
            )
            await asyncio.sleep(0.02)
        pcm = audio_ws.decode_audio_frame(
            await asyncio.wait_for(browser.audio.get(), 3)
        )
        assert any(pcm)
        relay = runtime.calls.resource_for(call.call_id, "relay")
        assert relay.right_tx_packets > 0 and relay.left_tx_packets > 0
        await asyncio.to_thread(registered_phones[2].hangup)
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(0.01)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_sip_pickup_after_forward_to_browser_retains_original_caller(
    trunks, registered_phones
):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    runtime.runtime.endpoints.register(
        PhoneEndpoint(
            endpoint_id="browser-target",
            name="browser-target",
            device_id="browser-device",
            kind=EndpointKind.BROWSER,
            availability="available",
            capabilities=frozenset({"audio", "dtmf"}),
        )
    )
    runtime.roster.append(
        RosterEntry(
            "browser-target",
            metadata={"local_ha": True, "endpoint_id": "browser-target"},
        )
    )
    runtime.hass.states.async_set(
        "sensor.voip_phonebook",
        "ready",
        {"roster_json": dump_roster_json(runtime.roster)},
    )
    caller, original, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    session = next(iter(runtime.calls.sessions.values()))
    await runtime.calls.forward_call(
        call_id=session.call_id, destination="browser-target", on_failure="terminate"
    )
    await wait_for(
        lambda: session.owner == "ha_softphone" and session.state == "ringing"
    )
    await asyncio.to_thread(picker.dial, "pickup.browser-target")
    assert (
        runtime.calls.get_session(session.call_id) is session
        and session.state == "in_call"
    )
    relay = runtime.calls.resource_for(session.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20)
    assert caller.read().count("Call established") == 1
    await asyncio.to_thread(picker.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("picker_kind", ["sip", "browser"])
async def test_directed_pickup_of_parking_return_keeps_answered_caller(
    trunks, registered_phones, monkeypatch, picker_kind
):
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "parking": {
                "enabled": True,
                "timeout": 1,
                "return_timeout": 10,
                "return_destination": "transfer_b",
            },
        },
    )
    caller, original, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.dial, "700")
    session = next(iter(runtime.calls.sessions.values()))
    source_transport = runtime.calls.resource_for(session.call_id, "relay").transport
    await wait_for(
        lambda: session.owner == "router"
        and any(
            leg.endpoint_id == "transfer_b" and leg.state == "remote_ringing"
            for leg in session.legs.values()
        )
    )
    assert runtime.runtime.endpoints.get("transfer_b").active_call_id == session.call_id
    if picker_kind == "browser":
        from unittest.mock import AsyncMock
        from custom_components.voip_stack import softphone_originate
        from custom_components.voip_stack.phone_endpoint import (
            PhoneEndpoint,
            EndpointKind,
        )
        from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id="browser-picker",
                name="browser-picker",
                device_id="browser-picker-device",
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        runtime.roster.append(
            RosterEntry(
                "browser-picker",
                metadata={"local_ha": True, "endpoint_id": "browser-picker"},
            )
        )
        runtime.hass.states.async_set(
            "sensor.voip_phonebook",
            "ready",
            {"roster_json": dump_roster_json(runtime.roster)},
        )
        monkeypatch.setattr(
            softphone_originate,
            "_ha_advertise_host",
            AsyncMock(return_value="127.0.0.1"),
        )
        monkeypatch.setattr(
            softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
        )
        await runtime.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": "browser-picker-device",
                "destination": "pickup.transfer_b",
                "media_client_id": "picker-owner",
            },
            blocking=True,
        )
        assert (
            runtime.runtime.endpoints.get("browser-picker").active_call_id
            == session.call_id
        )
    else:
        await asyncio.to_thread(picker.dial, "pickup.transfer_b")
    await wait_for(lambda: session.state == "in_call")
    if picker_kind == "sip":
        relay = runtime.calls.resource_for(session.call_id, "relay")
        assert relay.left_transport is source_transport
        await wait_for(
            lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20
        )
    assert caller.read().count("Call established") == 1
    assert "Call established" not in original.read()
    await asyncio.to_thread(caller.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")
