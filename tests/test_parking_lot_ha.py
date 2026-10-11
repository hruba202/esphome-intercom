"""Slots share the call session's cleanup and arbitrate pickup atomically."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.parking_lot import ParkingLot, ParkingError
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent

pytestmark = pytest.mark.ha


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
