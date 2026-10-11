"""Actual HA callback scheduling must wake browser media on its event loop."""

import asyncio
import pytest

pytestmark = pytest.mark.ha


async def test_browser_media_end_notification_runs_on_ha_loop(hass,monkeypatch):
    import threading
    from custom_components.voip_stack import media_call_lifetime
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    monkeypatch.setattr(media_call_lifetime,'_ha_softphone_store',lambda *_args:{'call_id':'call','state':'in_call'})
    ended,unsubscribe=media_call_lifetime.listen_for_media_call_end(hass,'call','browser')
    original_set=ended.set
    threads=[]
    def observed_set():
        threads.append(threading.get_ident())
        # Preserve the actual callback dispatch, but keep the failing witness
        # from corrupting an asyncio waiter when HA dispatches it to a worker.
        hass.loop.call_soon_threadsafe(original_set)
    monkeypatch.setattr(ended,'set',observed_set)
    try:
        hass.bus.async_fire(CALL_EVENT,{'call_id':'call','endpoint_id':'browser','state':'idle'})
        await asyncio.wait_for(ended.wait(),2)
        assert threads==[threading.get_ident()]
    finally:
        unsubscribe()

