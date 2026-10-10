"""Independent registered SIP phones exercise consultation and transfers."""

import asyncio
import shutil

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
module_path /usr/lib/baresip/modules
module stdio.so
module menu.so
module g711.so
module auconv.so
module auresamp.so
module ausine.so
module aubridge.so
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
            await asyncio.to_thread(peer.close)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(0.01)
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.relays_snapshot()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_phone_consultation_preserves_original_call(registered_phones, trunks):
    lab = trunks
    caller, operator, destination = registered_phones
    await asyncio.to_thread(caller.dial, "transfer_b")
    await asyncio.to_thread(operator.wait_for, "Call established", 5)
    original = lab.runtime.endpoints.get("transfer_b").active_call_id
    assert original
    start = len(operator.read())
    target = f'sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
    await asyncio.to_thread(operator.command, f"/atransferstart {target}")
    async with asyncio.timeout(3):
        while "Call established" not in operator.read()[start:]:
            assert "486" not in operator.output[start:], operator.output[start:]
            await asyncio.sleep(0.01)
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

