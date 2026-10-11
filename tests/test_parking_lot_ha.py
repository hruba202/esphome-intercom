"""Slots share the call session's cleanup and arbitrate pickup atomically."""

import asyncio

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
