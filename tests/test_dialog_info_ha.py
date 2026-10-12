"""BLF uses canonical call ownership and the existing SIP subscription owner."""

import asyncio
import time
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.call_projection import publish_bridge_projection
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_registrar import SipRegistrar, SipAccount, SipRegistration
from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha
NS = {"d": "urn:ietf:params:xml:ns:dialog-info"}


def subscribe(*, tag="", event="dialog", expires=300, accept="application/dialog-info+xml", cseq=1, call_id="watch-101", target="101"):
    return sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{target}@127.0.0.1", [
        ("From", "<sip:watcher@127.0.0.1>;tag=watcher"),
        ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
        ("Contact", "<sip:watcher@127.0.0.3:5060>"),
        ("Call-ID", call_id), ("CSeq", f"{cseq} SUBSCRIBE"),
        ("Event", event), ("Expires", str(expires)), ("Accept", accept),
    ]))


@pytest.fixture
async def blf(lab):
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    lab.runtime.endpoints.update("sip:bedroom0", extension="101")
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("watcher", "Watcher", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    registrar.registrations["watcher"] = SipRegistration(
        username="watcher", contact_uri="sip:watcher@127.0.0.3:5060", source_host="127.0.0.3",
        source_port=5060, transport="UDP", expires_at=time.time() + 300,
    )
    methods, notifications = SipApplicationMethods(lab.hass, registrar), asyncio.Queue()
    async def send(_request, _address, result):
        await notifications.put(result.follow_up)
    yield methods, notifications, send
    await methods.stop()
    assert not methods.tasks and methods.dialog_unsubscribe is None and methods.endpoint_unsubscribe is None


async def test_blf_tracks_ringing_answer_multiple_calls_and_final_idle(lab, blf):
    methods, notifications, send = blf
    initial = await methods.handle(subscribe(), ("127.0.0.3", 5060), "UDP", send)
    assert initial.status == 200
    root = ElementTree.fromstring(initial.follow_up.body)
    assert root.attrib == {"entity": "sip:101@127.0.0.1", "version": "0", "state": "full"}
    assert not root.findall("d:dialog", NS)
    first = lab.calls.upsert("first", state="ringing", owner="router", route_kind="sip",
                             source_endpoint_id="other", dest_endpoint_id="sip:bedroom0")
    lab.calls.claim_endpoint(first.call_id, "sip:bedroom0", role="destination")
    ringing = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert ringing.find("d:dialog/d:state", NS).text == "early"
    lab.calls.transition(first.call_id, state="in_call")
    publish_bridge_projection(lab.hass, first)
    answered = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert answered.find("d:dialog/d:state", NS).text == "confirmed"
    publish_bridge_projection(lab.hass, first)
    await lab.hass.async_block_till_done()
    assert notifications.empty()
    second = lab.calls.upsert("second", state="connecting", owner="router", route_kind="sip",
                              source_endpoint_id="sip:bedroom0")
    lab.calls.claim_endpoint(second.call_id, "sip:bedroom0", role="source")
    publish_bridge_projection(lab.hass, second)
    concurrent = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert len(concurrent.findall("d:dialog", NS)) == 2
    await lab.calls.terminate_call_wait(first.call_id, reason="remote_hangup")
    remaining = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert len(remaining.findall("d:dialog", NS)) == 1
    await lab.calls.terminate_call_wait(second.call_id, reason="remote_hangup")
    idle = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert not idle.findall("d:dialog", NS)
    assert [int(item.attrib["version"]) for item in (root, ringing, answered, concurrent, remaining, idle)] == list(range(6))


async def test_blf_refresh_keeps_dialog_version_and_removal_terminates_subscription(lab, blf):
    methods, notifications, send = blf
    first = await methods.handle(subscribe(expires=7200), ("127.0.0.3", 5060), "UDP", send)
    assert dict(first.headers)["Expires"] == "3600"
    refreshed = await methods.handle(subscribe(tag=first.to_tag, event="DIALOG", cseq=2), ("127.0.0.3", 5060), "UDP", send)
    assert refreshed.status == 200 and refreshed.to_tag == first.to_tag
    assert len(methods.subscriptions) == 1
    assert ElementTree.fromstring(refreshed.follow_up.body).attrib["version"] == "1"
    lab.runtime.endpoints.remove("sip:bedroom0")
    final = await asyncio.wait_for(notifications.get(), 1)
    assert dict(final.headers)["Subscription-State"] == "terminated;reason=noresource"
    assert not methods.subscriptions
    assert methods.dialog_unsubscribe is None and methods.endpoint_unsubscribe is None


@pytest.mark.parametrize("changes,status", [
    ({"tag": "unknown"}, 481), ({"accept": "application/pidf+xml"}, 406),
    ({"event": "dialog;from-tag=alone"}, 400), ({"expires": -1}, 400),
])
async def test_invalid_blf_subscription_does_not_create_state(blf, changes, status):
    methods, _notifications, send = blf
    result = await methods.handle(subscribe(**changes), ("127.0.0.3", 5060), "UDP", send)
    assert result.status == status
    assert not methods.subscriptions and methods.dialog_unsubscribe is None


async def test_dialog_filter_uses_monitored_phone_tags_and_does_not_mix_dialogs(lab, blf):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    methods, _notifications, send = blf
    lab.calls.upsert("parent", state="in_call", owner="bridge", source_endpoint_id="other",
                     dest_endpoint_id="sip:bedroom0")
    lab.calls.add_leg("parent", "wire", role="callee", state="in_call", endpoint_id="sip:bedroom0")
    client = SimpleNamespace(dialog=object(), dialog_ids=sip.SipDialogIds("wire", "ha-tag", "phone-tag"),
                             on_refer=None, terminate=AsyncMock(), close=AsyncMock())
    lab.calls.attach_sip_client("parent", "wire", client, state="in_call")
    lab.calls.claim_endpoint("parent", "sip:bedroom0", role="destination")
    for index, remote in enumerate(("ha-tag", "wrong")):
        result = await methods.handle(subscribe(
            call_id=f"filtered-{index}",
            event=f'dialog;call-id="wire";to-tag=phone-tag;from-tag={remote}',
        ), ("127.0.0.3", 5060), "UDP", send)
        assert result.status == 200
        root = ElementTree.fromstring(result.follow_up.body)
        dialogs = root.findall("d:dialog", NS)
        if index == 0:
            assert len(dialogs) == 1
            assert dialogs[0].attrib["local-tag"] == "phone-tag"
            assert dialogs[0].attrib["remote-tag"] == "ha-tag"
        else:
            assert dialogs == []


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_c"}], indirect=True)
async def test_sipp_observes_baresip_ring_answer_hangup_and_resource_removal(registered_phones, trunks, tmp_path):
    import shutil
    import socket
    from pathlib import Path

    executable = shutil.which("sipp")
    if executable is None:
        pytest.skip("SIPp is not installed")
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("blfwatch", "BLF watcher", "isolated-test-only")])
    lab.runtime.endpoints.update("transfer_c", extension="101")
    methods = lab.calls.component("sip_application")
    scenario = Path(__file__).parent / "fixtures/sipp/dialog-subscription.xml"
    messages = tmp_path / "dialog-messages.log"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        executable, f'127.0.0.1:{lab.runtime.transport_config["sip_port"]}',
        "-sf", str(scenario), "-i", "127.0.0.1", "-p", str(port), "-m", "1",
        "-trace_msg", "-message_file", str(messages), "-trace_err", "-error_file", str(tmp_path / "dialog-errors.log"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=tmp_path,
    )
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                if process.returncode is not None:
                    raise AssertionError((await process.communicate())[0].decode())
                await asyncio.sleep(0.01)
    def captured():
        return messages.read_text() if messages.exists() else ""
    try:
        await wait_for(lambda: bool(methods.subscriptions))
        caller, _operator, destination = registered_phones
        await asyncio.to_thread(caller.dial, "transfer_c", wait_for="180 Ringing")
        await wait_for(lambda: "early</state>" in captured())
        await asyncio.to_thread(destination.command, "/accept")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        await wait_for(lambda: "confirmed</state>" in captured())
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not lab.calls.sessions)
        lab.runtime.endpoints.remove("transfer_c")
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, output.decode()
        documents = [ElementTree.fromstring(line.strip()) for line in captured().splitlines()
                     if line.strip().startswith("<dialog-info")]
        assert len(documents) >= 4
        versions = [int(item.attrib["version"]) for item in documents]
        assert versions == list(range(len(documents)))
        assert not documents[0].findall("d:dialog", NS) and not documents[-1].findall("d:dialog", NS)
        assert not methods.subscriptions
    finally:
        if process.returncode is None:
            process.terminate()
            await process.communicate()


async def test_parking_slot_blf_tracks_reservation_without_a_fake_phone(lab, blf):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    entry=config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    methods, notifications, send=blf
    count=len(lab.runtime.endpoints.endpoints)
    result=await methods.handle(subscribe(target='701'),('127.0.0.3',5060),'UDP',send)
    assert result.status==200
    assert not ElementTree.fromstring(result.follow_up.body).findall('d:dialog',NS)
    lot=get_parking_lot(lab.hass)
    session=lab.calls.upsert('parked-call',state='in_call',owner='parking')
    session.claim_answer(session.token,lambda:True)
    parked=lot.reserve(session)
    busy=ElementTree.fromstring((await asyncio.wait_for(notifications.get(),1)).body)
    assert busy.find('d:dialog/d:state',NS).text=='confirmed'
    lot.release(parked)
    idle=ElementTree.fromstring((await asyncio.wait_for(notifications.get(),1)).body)
    assert not idle.findall('d:dialog',NS)
    assert len(lab.runtime.endpoints.endpoints)==count
    result=await methods.handle(subscribe(target='701',tag=result.to_tag,cseq=2,expires=0),('127.0.0.3',5060),'UDP',send)
    assert result.status==200
    assert methods.parking_unsubscribe is None and methods.dialog_unsubscribe is None
    await lab.calls.terminate_call_wait(session.call_id,reason='test_complete')
