"""Slots share the call session's cleanup and arbitrate pickup atomically."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.parking_lot import ParkingLot, ParkingError
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent
from .test_sip_phone_transfers_ha import registered_phones
from .test_trunk_calls_ha import trunks, lab
from .test_voicemail_ha import custom_integrations

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("hangup_side", [0, 1])
async def test_real_sip_parking_pickup_keeps_audio_and_releases_resources(registered_phones, trunks, tmp_path, hangup_side):
    import math
    import struct
    import wave
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, picker, _other = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    assert list(lot.calls) == ["701"]
    parked = lot.calls["701"]
    source_id = parked.session.call_id
    media = runtime.calls.resource_for(source_id, "relay")
    source_transport = media.transport
    await asyncio.to_thread(picker.dial, "701")
    await wait_for(lambda: not lot.calls)
    relay = runtime.calls.resource_for(source_id, "relay")
    assert relay.left_transport is source_transport
    assert len(runtime.calls.sessions) == 1
    assert runtime.runtime.rtp_port_pool['used'] == {relay.left_port, relay.right_port}
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    if hangup_side == 1:
        left_contract = relay.left
        await asyncio.to_thread(picker.command, "/hold")
        await wait_for(lambda: not relay.right.can_receive)
        assert relay.left is left_contract
        await asyncio.to_thread(picker.command, "/resume")
        await wait_for(lambda: relay.right.can_receive)
        left_count, right_count = relay.left_tx_packets, relay.right_tx_packets
        await wait_for(lambda: relay.left_tx_packets >= left_count + 50 and relay.right_tx_packets >= right_count + 50)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 550), ("transfer_b", 450)):
        found = False
        for path in (tmp_path/name).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                samples=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))[-1600:]
            if len(samples)<800:
                continue
            energies={frequency: abs(sum(value*complex(math.cos(2*math.pi*frequency*index/rate),math.sin(2*math.pi*frequency*index/rate)) for index,value in enumerate(samples))) for frequency in (450,550)}
            found |= energies[expected] > energies[1000-expected]*5 and energies[expected]>10000
        assert found, f'{name} did not receive the opposite phone audio after pickup'


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{}, {"manual": "transfer_c"}], indirect=True)
async def test_parking_timeout_uses_existing_return_route(registered_phones, trunks, request):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {
        "enabled": True, "timeout": 1, "return_timeout": 1,
        "return_destination": "transfer_c",
    }})
    caller, _picker, destination = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    await wait_for(lambda: "Incoming call" in destination.read() or "Call established" in destination.read())
    await wait_for(lambda: not lot.calls)
    if "manual" in request.node.callspec.params["registered_phones"]:
        await wait_for(lambda: not runtime.calls.sessions)
        assert "Call established" not in destination.read()
    else:
        await asyncio.to_thread(destination.wait_for, "Call established", 3)
        await wait_for(lambda: any(s.owner == "bridge" and s.state == "in_call" for s in runtime.calls.sessions.values()))
        session = next(iter(runtime.calls.sessions.values()))
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets >= 20 and relay.right_tx_packets >= 20)
        assert runtime.calls.resource_for(session.call_id, "parking") is None
        # Returning a call must retire the old application so another park
        # acquires a fresh slot and controller on the same live dialog.
        await asyncio.to_thread(destination.command,
            f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda: "701" in lot.calls)
        assert lot.calls["701"].session is session
        assert runtime.calls.resource_for(session.call_id, "parking") is not None
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not lot.calls
    assert not runtime.runtime.rtp_port_pool['used']


async def test_slot_contention_rollback_cleanup_and_reuse():
    owner = SipEndpointRuntime()
    owner.activate()
    lot = ParkingLot(("701",))
    first = owner.upsert("first", state="in_call", owner="parking")
    second = owner.upsert("second", state="in_call", owner="parking")
    slot = lot.reserve(first)
    assert lot.reserve(first) is slot
    with pytest.raises(ParkingError, match="full"):
        lot.reserve(second)
    claim = lot.claim("701", "pickup-a")
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "pickup-b")
    claim.rollback()
    successor = lot.claim("701", "pickup-b")
    claim.rollback()
    assert successor.current
    successor.commit()
    replacement = lot.reserve(second)
    await first.terminate(TerminationIntent("remote_hangup"))
    assert lot.calls["701"] is replacement
    with pytest.raises(ParkingError):
        successor.commit()
    await lot.close()
    assert not lot.calls
    await owner.shutdown()


async def test_timeout_and_pickup_cannot_both_control_the_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    expired = asyncio.Event()
    release = asyncio.Event()
    async def on_return(claim):
        expired.set()
        await release.wait()
        claim.commit()
    lot.arm_return(parked, 0.01, on_return)
    await asyncio.wait_for(expired.wait(), 1)
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "late-pickup")
    release.set()
    await parked.timer
    assert not lot.calls
    assert session.live
    await owner.shutdown()


async def test_remote_hangup_revokes_pending_pickup_and_owned_timer():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    async def unexpected(_claim):
        pytest.fail("A terminated call returned from parking")
    lot.arm_return(parked, 60, unexpected)
    claim = lot.claim("701", "pickup")
    await session.terminate(TerminationIntent("remote_hangup"))
    assert not lot.calls and parked.timer.done()
    with pytest.raises(ParkingError):
        claim.commit()
    claim.rollback()
    await owner.shutdown()


async def test_failed_pickup_after_deadline_still_returns_the_parked_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    returned = asyncio.Event()
    async def on_return(claim):
        claim.commit()
        returned.set()
    lot.arm_return(parked, 0.01, on_return)
    claim = lot.claim("701", "pickup")
    await asyncio.sleep(0.02)
    assert not returned.is_set() and claim.current
    claim.rollback()
    await asyncio.wait_for(returned.wait(), 1)
    await parked.timer
    assert not lot.calls
    await owner.shutdown()


@pytest.mark.parametrize("hangup_side", ["caller", "pickup"])
async def test_joined_inbound_dialog_uses_parent_cleanup(monkeypatch, hangup_side):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import SipTerminationDisposition

    owner = SipEndpointRuntime()
    owner.activate()
    dialogs = {"caller", "pickup"}
    signaled = []
    async def bye(identity):
        if identity not in dialogs:
            return False
        dialogs.remove(identity)
        signaled.append(identity)
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity in dialogs else None,
                             async_send_bye=bye)
    async def signal(current, intent):
        if intent.sip_disposition != SipTerminationDisposition.NONE:
            await bye(current)
    owner.bind_termination_signaler(signal)
    session = owner.upsert("caller", state="in_call", owner="bridge")
    leg = owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    assert owner.resolve_session_id("pickup") == "caller"
    assert not session.named_tasks and leg.dialog is None
    assert len(owner.sessions) == 1
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    dialogs.remove(hangup_side)  # The native listener has accepted the remote BYE.
    await endpoint_termination.EndpointTerminationHandler(None).handle(hangup_side)
    assert not dialogs and not owner.sessions and not owner.leg_index
    assert signaled == ["pickup" if hangup_side == "caller" else "caller"]
    await owner.shutdown()


async def test_retired_inbound_leg_cannot_terminate_surviving_call(monkeypatch):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import LegPhase

    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity, async_send_bye=bye)
    leg = owner.attach_inbound_dialog("caller", "retired", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    leg.phase = LegPhase.CLOSING
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    await endpoint_termination.EndpointTerminationHandler(None).handle("retired")
    assert session.live and owner.sessions["caller"] is session
    await owner.shutdown()


async def test_inbound_dialog_rejects_unknown_stale_and_already_owned_identity():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity == "pickup" else None,
                             async_send_bye=bye)
    with pytest.raises(RuntimeError):
        owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                    expected_generation=session.generation + 1)
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("caller", "unknown", server, endpoint_id="desk",
                                    expected_generation=session.generation)
    assert not session.legs and not owner.leg_index
    owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                expected_generation=session.generation)
    other = owner.upsert("other", state="in_call", owner="bridge")
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("other", "pickup", server, endpoint_id="other-desk",
                                    expected_generation=other.generation)
    assert not other.legs and owner.resolve_session_id("pickup") == "caller"
    await owner.shutdown()


@pytest.mark.parametrize('conflict',[False,True])
async def test_parking_configuration_validates_public_routes(lab, custom_integrations, conflict):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.phonebook_services import _validate_contact_namespace
    from custom_components.voip_stack.roster import RosterEntry

    entry=config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry,data={})
    if conflict:
        lab.hass.config_entries.async_add_subentry(entry,ConfigSubentry(subentry_type='contact',title='701',unique_id='existing-slot',data={'id':'existing-slot','name':'701','number':'12345'}))
    lab.hass.config.components.update({'assist_pipeline','ffmpeg','http','lovelace','media_source','network'})
    form=await lab.hass.config_entries.flow.async_init('voip_stack',context={'source':'reconfigure','entry_id':entry.entry_id})
    form=await lab.hass.config_entries.flow.async_configure(form['flow_id'],form['data_schema']({'parking_enabled':True}))
    assert form['step_id']=='parking'
    data=form['data_schema']({})
    result=await lab.hass.config_entries.flow.async_configure(form['flow_id'],data)
    if conflict:
        assert result['errors']=={'base':'parking_route_conflict'}
        assert not entry.data.get('parking')
    else:
        assert result['type']=='abort'
        assert entry.data['parking']['enabled'] and entry.data['parking']['slots'][0]=='701'
        with pytest.raises(ValueError,match='conflicts'):
            _validate_contact_namespace(lab.hass,[RosterEntry('future-conflict',name='701')])


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('retained_outgoing',[False,True])
async def test_browser_pickup_uses_existing_call_and_actual_rtp(registered_phones,trunks,monkeypatch,tmp_path,retained_outgoing):
    import math
    import struct
    import wave
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint,EndpointKind
    from custom_components.voip_stack import softphone_originate,audio_ws_view,audio_ws
    from custom_components.voip_stack.roster import RosterEntry,dump_roster_json

    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    endpoint=PhoneEndpoint(endpoint_id='browser-pickup',name='Pickup Browser',device_id='pickup-device',kind=EndpointKind.BROWSER,
                           availability='available',capabilities=frozenset({'audio','dtmf'}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.roster.append(RosterEntry('Pickup Browser',metadata={'local_ha':True,'endpoint_id':endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate,'_ha_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():await asyncio.sleep(0.01)
    if retained_outgoing:
        await asyncio.to_thread(registered_phones[0].dial,'transfer_b')
        await wait_for(lambda:any(s.state=='in_call' for s in runtime.calls.sessions.values()))
        await asyncio.to_thread(registered_phones[0].command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda:'701' in get_parking_lot(runtime.hass).calls)
        await wait_for(lambda:not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    else:
        await asyncio.to_thread(registered_phones[0].dial,'700')
    lot=get_parking_lot(runtime.hass)
    source_id=lot.calls['701'].session.call_id
    clients_before=set(runtime.calls.sip_clients_snapshot())
    await runtime.hass.services.async_call('voip_stack','call',{
        'device_id':endpoint.device_id,'destination':'701','media_client_id':'parking-browser-media-owner',
    },blocking=True)
    assert not lot.calls and len(runtime.calls.sessions)==1
    assert runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id==source_id
    assert set(runtime.calls.sip_clients_snapshot())==clients_before
    session=audio_ws_view._active_softphone_media_session(runtime.hass,endpoint.endpoint_id)
    assert session.call_id==source_id
    class Browser:
        def __init__(self):
            self.audio=asyncio.Queue();self.messages=asyncio.Queue();self.ready=asyncio.Event();self.closed=False
        def force_close(self):self.closed=True
        def __aiter__(self):return self
        async def __anext__(self):return await self.messages.get()
        async def send_json(self,_value):self.ready.set()
        async def send_bytes(self,value):await self.audio.put(value)
    browser=Browser()
    task=asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass,browser,session,endpoint_id=endpoint.endpoint_id))
    try:
        await asyncio.wait_for(browser.ready.wait(),2)
        pcm=audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(),2))
        samples=struct.unpack(f'<{len(pcm)//2}h',pcm)
        assert max(abs(value) for value in samples)>500
        if retained_outgoing:
            await asyncio.to_thread(registered_phones[1].command,'/hold')
            await wait_for(lambda:session.local_audio_direction=='recvonly')
            await asyncio.to_thread(registered_phones[1].command,'/resume')
            await wait_for(lambda:session.local_audio_direction=='sendrecv')
        fmt=session.send_format.audio_format
        frame=struct.pack(f'<{fmt.nominal_frame_samples}h',*(int(8000*math.sin(2*math.pi*650*i/fmt.sample_rate)) for i in range(fmt.nominal_frame_samples)))
        for _ in range(30):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY,data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(fmt.frame_ms/1000)
        from homeassistant.exceptions import ServiceValidationError
        original_owner = runtime.calls.get_session(source_id)
        with pytest.raises(ServiceValidationError, match="generation"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': endpoint.device_id,
                 'expected_generation': original_owner.generation + 1}, blocking=True)
        assert not lot.calls and session.handoff_transport is not None
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': endpoint.device_id}, blocking=True, return_response=True)
        assert response['call_id'] == source_id and response['slot'] == '701'
        application = runtime.calls.resource_for(source_id, 'parking')
        await asyncio.wait_for(task,2)
        assert browser.closed
        assert lot.calls['701'].session.call_id == source_id
        assert endpoint.endpoint_id not in lot.calls['701'].session.endpoint_claims
        assert application.media.transport is not None
        # A real SIP phone retrieves the same caller after browser parking.
        await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda:not lot.calls)
        relay=runtime.calls.resource_for(source_id,'relay')
        await wait_for(lambda:relay.left_tx_packets >= 30 and relay.right_tx_packets >= 30)
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda:not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        heard=False
        for path in (tmp_path/('transfer_b' if retained_outgoing else 'transfer_a')).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                values=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))
            if len(values)<800:continue
            power={frequency:max(abs(sum(value*complex(math.cos(2*math.pi*frequency*i/rate),math.sin(2*math.pi*frequency*i/rate)) for i,value in enumerate(values[start:start+800]))) for start in range(0,len(values)-799,400)) for frequency in (450,650)}
            heard |= power[650]>power[450]*5 and power[650]>10000
        assert heard, 'The SIP caller did not receive browser PCM after pickup'
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('via_service', [False, True])
async def test_sip_refer_parks_established_caller_without_redial(registered_phones,trunks,via_service):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,operator,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    source_id=original.call_id
    relay=runtime.calls.resource_for(source_id,'relay')
    transport=relay.left_transport
    if via_service:
        runtime.runtime.endpoints.update('transfer_b', device_id='operator-device')
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'operator-device'}, blocking=True, return_response=True)
        assert response['slot'] == '701' and response['call_id'] == source_id
    else:
        await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert lot.calls['701'].session is original
    assert runtime.calls.resource_for(source_id,'relay').transport is transport
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(source_id,'relay')
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    # The pickup leg is now a native incoming dialog. It can park the same
    # caller again without creating a new source call or leaking another pair.
    await asyncio.to_thread(picker.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_c').active_call_id)
    assert lot.calls['701'].session is original
    await asyncio.to_thread(operator.dial,'701')
    await wait_for(lambda: not lot.calls)
    assert len(runtime.runtime.rtp_port_pool['used'])==2
    await asyncio.to_thread(operator.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
async def test_caller_can_park_answered_outgoing_peer_and_keep_its_dialog(registered_phones,trunks):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks;entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    operator,remaining,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():await asyncio.sleep(0.01)
    await asyncio.to_thread(operator.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    client=runtime.calls.sip_client_for(runtime.calls.bridge_link_for(original.call_id))
    transport=runtime.calls.resource_for(original.call_id,'relay').right_transport
    await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    current=lot.calls['701'].session
    assert current is not original and current.state=='in_call'
    assert runtime.calls.sip_client_for(client.dialog_ids.call_id) is client
    media=runtime.calls.resource_for(current.call_id,'relay')
    assert media.transport is transport and remaining.read().count('Call established')==1
    await asyncio.to_thread(remaining.command,'/hold')
    await wait_for(lambda: not media.can_send)
    await asyncio.to_thread(remaining.command,'/resume')
    await wait_for(lambda: media.can_send)
    await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(current.call_id,'relay')
    assert relay.left_transport is transport
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    await asyncio.to_thread(remaining.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
async def test_failed_pickup_handoff_keeps_original_call_and_allows_retry(registered_phones,trunks,monkeypatch):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks;entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,first,second=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'700')
    lot=get_parking_lot(runtime.hass);parked=lot.calls['701']
    media=runtime.calls.resource_for(parked.session.call_id,'relay')
    transport=media.transport;protocol=transport.get_protocol();original_set=transport.set_protocol
    failures=[]
    def fail_once(value):
        if not failures:
            failures.append(True)
            raise RuntimeError('injected RTP ownership failure')
        original_set(value)
    monkeypatch.setattr(transport,'set_protocol',fail_once)
    await asyncio.to_thread(first.command,'/dial 701')
    await wait_for(lambda:failures and parked.claim is None and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert parked.session.live and media.transport is transport and transport.get_protocol() is protocol
    assert runtime.runtime.rtp_port_pool['used']==set(media.reservation.ports)
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(second.dial,'701')
    await wait_for(lambda:not lot.calls)
    await asyncio.to_thread(second.hangup)
    await wait_for(lambda:not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('parker', ['local-a', 'local-b'])
@pytest.mark.parametrize('pickup_kind', ['sip', 'browser', 'return-browser'])
async def test_local_browser_parking_keeps_other_phone_for_sip_pickup(registered_phones, trunks, parker, pickup_kind, monkeypatch):
    import struct
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    for name in ('local-a', 'local-b', 'local-c'):
        runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=name, name=name,
            device_id='device-' + name, kind=EndpointKind.BROWSER,
            availability='available', capabilities=frozenset({'audio', 'dtmf'})))
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(runtime.hass, 'local-a', 'local-b', caller_owner_id='owner-a')
    bridge.answer(call.call_id, 'local-b', 'owner-b')
    retained = 'local-b' if parker == 'local-a' else 'local-a'
    picker = parker if pickup_kind == 'return-browser' else 'local-c'
    from custom_components.voip_stack.local_call_media import LocalCallMedia
    from homeassistant.exceptions import ServiceValidationError
    original_start = LocalCallMedia.start
    async def fail_after_bind(media):
        await original_start(media)
        raise OSError("injected application startup failure")
    with monkeypatch.context() as fault:
        fault.setattr(LocalCallMedia, 'start', fail_after_bind)
        with pytest.raises(OSError, match="injected"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + parker}, blocking=True)
    assert bridge.get_call(call.call_id).callee_state.value == 'in_call'
    assert not get_parking_lot(runtime.hass).calls
    assert not runtime.runtime.rtp_port_pool['used']
    assert runtime.calls.resource_for(call.call_id, 'softphone_media')['local_bridge']
    response = await runtime.hass.services.async_call('voip_stack', 'park',
        {'device_id': 'device-' + parker}, blocking=True, return_response=True)
    with pytest.raises(ServiceValidationError, match="does not own"):
        await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'device-' + parker, 'call_id': call.call_id}, blocking=True)
    assert response['call_id'] == call.call_id
    assert bridge.get_call(call.call_id) is None
    assert not runtime.runtime.endpoints.get(parker).active_call_id
    assert runtime.runtime.endpoints.get(retained).active_call_id == call.call_id
    media = audio_ws_view._active_softphone_media_session(runtime.hass, retained)
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
    worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=retained))
    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        if pickup_kind != 'sip':
            from unittest.mock import AsyncMock
            from custom_components.voip_stack import softphone_originate
            from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
            runtime.roster.append(RosterEntry(picker, metadata={'local_ha': True, 'endpoint_id': picker}))
            runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
            monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
            monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))
            await runtime.hass.services.async_call('voip_stack', 'call',
                {'device_id': 'device-' + picker, 'destination': '701', 'media_client_id': 'owner-c'}, blocking=True)
            resumed = bridge.get_call(call.call_id)
            assert resumed is not None and resumed.caller_endpoint_id == retained
            await asyncio.wait_for(worker, 2)
            assert not runtime.runtime.rtp_port_pool['used']
            source_lease = bridge.acquire_media(call.call_id, retained, 'owner-b' if retained == 'local-b' else 'owner-a')
            target_lease = bridge.acquire_media(call.call_id, picker, 'owner-c')
            left_pcm = struct.pack('<480h', *([7000] * 480))
            right_pcm = struct.pack('<480h', *([9000] * 480))
            bridge.send_audio(call.call_id, retained, source_lease.token, left_pcm)
            bridge.send_audio(call.call_id, picker, target_lease.token, right_pcm)
            assert bridge.receive_audio_nowait(call.call_id, picker, target_lease.token) == left_pcm
            assert bridge.receive_audio_nowait(call.call_id, retained, source_lease.token) == right_pcm
            # The restored ordinary browser call can be parked in the opposite direction.
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + retained}, blocking=True)
            assert get_parking_lot(runtime.hass).calls['701'].session.call_id == call.call_id
            assert runtime.calls.get_session(call.call_id).metadata['source_endpoint_id'] == picker
            await runtime.hass.services.async_call('voip_stack', 'hangup',
                {'device_id': 'device-' + picker}, blocking=True)
            await wait_for(lambda: not runtime.calls.sessions)
            assert not runtime.runtime.rtp_port_pool['used']
            return
        else:
            await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda: not get_parking_lot(runtime.hass).calls)
        frame = struct.pack('<h', 7000) * media.send_format.audio_format.nominal_frame_samples
        for _ in range(20):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(media.send_format.audio_format.frame_ms / 1000)
        async with asyncio.timeout(3):
            while True:
                pcm = audio_ws.decode_audio_frame(await browser.audio.get())
                if max(abs(value) for value in struct.unpack(f'<{len(pcm)//2}h', pcm)) > 1000:
                    break
        relay = runtime.calls.resource_for(call.call_id, 'relay')
        assert relay.left_rx_packets > 0 and relay.right_rx_packets > 0
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        await asyncio.wait_for(worker, 2)
        assert not runtime.runtime.rtp_port_pool['used']
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async_shutdown_local_softphone_bridge(runtime.hass)
