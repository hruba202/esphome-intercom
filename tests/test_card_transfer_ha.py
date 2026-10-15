"""The dashboard transfer action with independent SIP phones and RTP."""
import asyncio
import math
import struct
import wave
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack import softphone_originate

pytestmark = pytest.mark.ha


async def wait_for(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("incoming", [False, True])
async def test_browser_transfer_service_moves_peer_to_registered_phone(registered_phones, trunks, monkeypatch, tmp_path, reject, incoming):
    runtime = trunks
    endpoint = PhoneEndpoint(endpoint_id='transfer-browser', name='Operator', device_id='transfer-browser-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'dtmf'}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.roster.append(RosterEntry(endpoint.name, metadata={'local_ha': True, 'endpoint_id': endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))
    if incoming:
        await asyncio.to_thread(registered_phones[0].command, '/dial Operator')
        await wait_for(lambda: runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id)
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        await runtime.hass.services.async_call('voip_stack', 'answer', {
            'device_id': endpoint.device_id, 'call_id': call_id, 'media_client_id': 'transfer-browser-media',
        }, blocking=True)
        await wait_for(lambda: runtime.calls.get_session(call_id).state == 'in_call')
    else:
        await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': endpoint.device_id, 'destination': 'transfer_a', 'media_client_id': 'transfer-browser-media',
        }, blocking=True)
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
    runtime.runtime.endpoints.update('transfer_c', device_id='other-phone')
    with pytest.raises(ServiceValidationError, match='does not own'):
        await runtime.hass.services.async_call('voip_stack', 'transfer', {
            'device_id': 'other-phone', 'call_id': call_id, 'destination': 'transfer_b',
        }, blocking=True, return_response=True)
    if reject:
        runtime.runtime.endpoints.update('transfer_b', dnd=True)
    response = await runtime.hass.services.async_call('voip_stack', 'transfer', {
        'device_id': endpoint.device_id, 'call_id': call_id, 'destination': 'transfer_b',
    }, blocking=True, return_response=True)
    if reject:
        assert not response['success'] and response['status'] == 486, response
        assert runtime.calls.get_session(call_id).live
        if incoming:
            assert runtime.calls.component('udp_listener').remote_uri_for_call(call_id)
        else:
            assert runtime.calls.sip_client_for(call_id).dialog is not None
        await runtime.hass.services.async_call('voip_stack', 'hangup', {'device_id': endpoint.device_id}, blocking=True)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get('used')
        return
    assert response['success'], str(response)
    await wait_for(lambda: call_id not in runtime.calls.sessions)
    for peer in registered_phones[:2]:
        await asyncio.to_thread(peer.read)
        assert 'established' in peer.output.lower(), peer.output
    remaining = next(iter(runtime.calls.sessions.values()))
    relay = runtime.calls.resource_for(remaining.call_id, 'relay')
    await wait_for(lambda: relay.left_tx_packets >= 30 and relay.right_tx_packets >= 30)
    await asyncio.to_thread(registered_phones[0].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get('used')

    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 550), ("transfer_b", 450)):
        found = False
        for path in (tmp_path / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as audio:
                rate = audio.getframerate()
                samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))[-1600:]
            if len(samples) < 800:
                continue
            powers = {frequency: abs(sum(value * complex(
                math.cos(2 * math.pi * frequency * index / rate),
                math.sin(2 * math.pi * frequency * index / rate),
            ) for index, value in enumerate(samples))) for frequency in (450, 550, 650)}
            found |= max(powers, key=powers.get) == expected and powers[expected] > 10000
        assert found, f"{name} did not receive the transferred peer's audio"
