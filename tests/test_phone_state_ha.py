"""HA and SIP consumers read the same phone dialogs and mailbox state."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.helpers.trigger import TriggerConfig
from homeassistant.helpers.condition import ConditionConfig
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from custom_components.voip_stack.phone_state import phone_states, phone_state_observer
from custom_components.voip_stack.trigger import PhoneStateTrigger, CALL_EVENT
from custom_components.voip_stack.condition import (
    PhoneStateCondition,
    HasRingingCallCondition,
)
from custom_components.voip_stack.automation_context import current_execution

pytestmark = pytest.mark.ha


@pytest.fixture
async def phones(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    for identity, extension in (("kitchen", "101"), ("office", "102")):
        endpoints.register(
            PhoneEndpoint(
                endpoint_id=identity,
                name=identity.title(),
                kind="browser",
                device_id=identity + "-device",
                extension=extension,
                availability="available",
            )
        )
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    calls.bind_endpoint_registry(endpoints)
    entry.runtime_data = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=endpoints,
        phones=None,
        sip=calls,
    )
    yield SimpleNamespace(calls=calls, endpoints=endpoints, runtime=entry.runtime_data)
    await calls.shutdown()


async def test_phone_state_service_returns_same_calls_as_conditions(hass, phones):
    from custom_components.voip_stack import _async_register_services
    from custom_components.voip_stack.dialog_info import snapshot

    await _async_register_services(hass)
    session = phones.calls.upsert(
        "one",
        state="ringing",
        owner="router",
        caller="Office",
        callee="Kitchen",
        source_endpoint_id="office",
        dest_endpoint_id="kitchen",
    )
    phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
    response = await hass.services.async_call(
        "voip_stack",
        "get_phone_state",
        {"device_id": "kitchen-device"},
        blocking=True,
        return_response=True,
    )
    assert (
        response["state"] == "ringing" and response["calls"][0]["peer_name"] == "Office"
    )
    assert response["calls"][0]["call_id"] == session.call_id
    assert response["calls"][0]["generation"] == session.generation
    assert snapshot(phones.runtime, "kitchen")[0].peer_user == "102"
    for kind, options in (
        (PhoneStateCondition, {"phone": "kitchen-device", "state": "ringing"}),
        (HasRingingCallCondition, {"phone": "kitchen-device"}),
    ):
        condition = kind(hass, ConditionConfig(options=options))
        assert condition._async_check()
    response["calls"].clear()
    assert phone_states(hass)["kitchen"]["calls"]


async def test_state_trigger_keeps_exact_call_context_and_suppresses_duplicate_state(
    hass, phones
):
    seen, tasks = [], []
    subject = PhoneStateTrigger(
        hass,
        TriggerConfig(
            "voip_stack.phone_state_changed",
            options={"phone": "kitchen-device", "to": "ringing"},
        ),
    )

    def runner(payload, description, context):
        async def action():
            await asyncio.sleep(0)
            execution = current_execution.get()
            seen.append((execution.token, payload["phone"]["state"]))

        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    detach = await subject.async_attach_runner(runner)
    try:
        session = phones.calls.upsert(
            "one",
            state="ringing",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        await hass.async_block_till_done()
        hass.bus.async_fire(
            CALL_EVENT, {"call_id": session.call_id, "generation": session.generation}
        )
        await hass.async_block_till_done()
        await asyncio.gather(*tasks)
        assert seen == [(session.token, "ringing")]
        assert current_execution.get() is None
    finally:
        detach()
    assert not phone_state_observer(hass).listeners


async def test_one_observer_fans_out_and_retains_identity_after_rename(
    hass, phones, monkeypatch
):
    from custom_components.voip_stack import phone_state
    from custom_components.voip_stack.call_projection import publish_bridge_projection

    scans = []
    original = phone_state.snapshot_all

    def scan(runtime):
        scans.append(True)
        return original(runtime)

    monkeypatch.setattr(phone_state, "snapshot_all", scan)
    observer = phone_state_observer(hass)
    changes, dialogs, activity = [], [], []
    detach = [
        observer.subscribe(lambda payload, _: changes.append(payload)),
        observer.subscribe(dialogs.append, kind="dialog"),
        observer.subscribe(activity.append, kind="activity"),
    ]
    assert len(scans) == 1
    try:
        session = phones.calls.upsert(
            "one",
            state="ringing",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        publish_bridge_projection(hass, session)
        await hass.async_block_till_done()
        scans.clear()
        phones.endpoints.update("kitchen", name="Dining room")
        await hass.async_block_till_done()
        assert len(scans) == 0
        assert observer.previous["kitchen"]["name"] == "Dining room"
        assert observer.previous["kitchen"]["calls"][0]["call_id"] == session.call_id
        assert changes[-1]["endpoint_id"] == "kitchen"
        assert dialogs and activity
        scans.clear()
        hass.bus.async_fire(
            CALL_EVENT, {"call_id": session.call_id, "generation": session.generation}
        )
        await hass.async_block_till_done()
        assert len(scans) == 1
    finally:
        for unsubscribe in detach:
            unsubscribe()
    scans.clear()
    hass.bus.async_fire(CALL_EVENT, {"call_id": "one"})
    await hass.async_block_till_done()
    assert scans == []
    assert not observer.unsubscribers


async def test_claim_does_not_emit_phone_state_before_canonical_call_event(
    hass, phones
):
    from custom_components.voip_stack.call_projection import publish_bridge_projection

    changes = []
    observer = phone_state_observer(hass)
    detach = observer.subscribe(
        lambda payload, _: changes.append(
            (payload, phones.calls.event_context("one").state)
        )
    )
    try:
        session = phones.calls.upsert(
            "one",
            state="connecting",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        publish_bridge_projection(hass, session)
        phones.calls.transition(session.call_id, state="ringing")
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        await hass.async_block_till_done()
        assert changes == []
        phones.endpoints.update("kitchen", name="Dining room")
        await hass.async_block_till_done()
        assert changes[-1][0]["phone"]["state"] == "idle"
        changes.clear()
        publish_bridge_projection(hass, session)
        await hass.async_block_till_done()
        kitchen = [
            (payload, state)
            for payload, state in changes
            if payload["endpoint_id"] == "kitchen"
        ]
        assert len(kitchen) == 1
        assert kitchen[0][0]["phone"]["state"] == kitchen[0][1] == "ringing"
    finally:
        detach()


@pytest.mark.parametrize("outer_context", [False, True])
async def test_mailbox_change_does_not_bind_an_unrelated_active_call(
    hass, phones, monkeypatch, outer_context
):
    from custom_components.voip_stack import phone_state
    from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_CHANGED

    count = 0
    monkeypatch.setattr(
        phone_state,
        "mailbox_state",
        lambda _hass, _endpoint: {
            "enabled": True,
            "waiting": bool(count),
            "new": count,
            "old": 0,
        },
    )
    session = phones.calls.upsert(
        "active",
        state="in_call",
        owner="router",
        source_endpoint_id="office",
        dest_endpoint_id="kitchen",
    )
    phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
    seen, tasks = [], []
    subject = PhoneStateTrigger(
        hass,
        TriggerConfig(
            "voip_stack.mailbox_changed", options={"phone": "kitchen-device"}
        ),
    )

    def runner(payload, description, context):
        async def action():
            seen.append((current_execution.get(), payload["phone"]["mailbox"]["new"]))

        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    detach = await subject.async_attach_runner(runner)
    try:
        count = 1
        from custom_components.voip_stack.automation_context import CallExecution

        binding = (
            current_execution.set(CallExecution(session.token, {}))
            if outer_context
            else None
        )
        try:
            hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
        finally:
            if binding is not None:
                current_execution.reset(binding)
        await hass.async_block_till_done()
        await asyncio.gather(*tasks)
        assert seen == [(None, 1)]
    finally:
        detach()
