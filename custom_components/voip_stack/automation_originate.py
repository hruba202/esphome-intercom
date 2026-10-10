"""Originate an application-owned SIP call for a normal HA automation."""

from __future__ import annotations

import asyncio

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_call import AutomationCall
from .automation_context import current_execution
from .call_projection import publish_bridge_projection
from .core.sip import sip_default_port
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
from .endpoint_dialing import EndpointDialer, OutboundLegPolicy
from .endpoint_lifecycle import call_registry
from .endpoint_routing import EndpointRouteResolver, roster_from_peers
from .endpoint_registry import EndpointBusyError
from .endpoint_session import CleanupStage, TerminationIntent
from .fsm import CallState
from .local_call_media import LocalAudioContract, LocalCallMedia
from .outbound_attempts import async_close_outbound_leg
from .pbx_routing import roster_entry_for_target
from .peer_snapshot import async_advertise_host, async_build_peer_snapshot
from .phonebook_runtime import registered_roster_entries
from .runtime_data import require_runtime_data
from .sip_runtime import enable_reused_tcp_connection, uri_transport
from .trunk_policy import is_trunk_uri


async def _dial_attempt(hass, registry, session, attempt, destination, timeout):
    """Arbitrate an outgoing leg and route decisions with the existing fork."""
    client = attempt.client
    decision = hass.loop.create_future()
    registry.set_pending_route(session.call_id, {"future": decision, "outgoing_automation": True})

    async def dial():
        result = await client.invite(
            target=attempt.uri.user, target_display_name=destination,
            remote_host=attempt.uri.host, remote_sip_port=sip_default_port(attempt.uri),
            request_uri=str(attempt.uri), timeout=timeout,
        )
        if result in {"ringing", "calling", "remote_ringing"}:
            registry.transition(session.call_id, state=CallState.REMOTE_RINGING.value,
                                expected_generation=session.generation)
            publish_bridge_projection(hass, session, event_type="remote_ringing",
                                      direction="outgoing", route_kind="automation")
            result = await client.wait_for_final(timeout=timeout)
        if result == "in_call" and session.owner == "automation":
            registry.transition(session.call_id, state=CallState.IN_CALL.value,
                                expected_generation=session.generation)
        if result != "in_call" and not decision.done():
            decision.set_result({"action": "failed"})
        return DialOutcome(
            DialDisposition.ANSWERED if result == "in_call" and client.dialog is not None
            else DialDisposition.UNAVAILABLE, reason=result,
        )

    async def reroute():
        route = await decision
        return DialOutcome(DialDisposition.REROUTE if route.get("action") == "forward"
                           else DialDisposition.UNAVAILABLE)

    async def close(_mode):
        await registry.close_leg(session.call_id, client.dialog_ids.call_id, reason="route_replaced")
        attempt.ports.release()

    async def close_control(_mode):
        return None

    fork = DialForkController(session, (
        DialCandidate(client.dialog_ids.call_id, dial, close, endpoint_id=attempt.endpoint_id),
        DialCandidate("route-control", reroute, close_control, control=True),
    ), overall_timeout=timeout)
    try:
        result = await fork.run(lambda _candidate, _outcome: session.live and session.owner == "automation")
        return result.outcome, decision.result() if decision.done() and not decision.cancelled() else None
    finally:
        registry.take_pending_route(session.call_id)


async def async_originate_automation_call(call: ServiceCall) -> dict:
    """Return a call token only after final acceptance and local media binding."""
    await async_require_service_admin(call.hass, call)
    if call.data.get("device_id"):
        raise ServiceValidationError("Choose either a calling phone or an Automation contact")
    if call.data.get("send_video"):
        raise ServiceValidationError("Outgoing automation calls support audio only")
    if current_execution.get() is not None:
        raise ServiceValidationError("Start outgoing automation calls from a normal Home Assistant automation")
    hass = call.hass
    runtime = require_runtime_data(hass)
    registry = call_registry(hass)
    peers = await async_build_peer_snapshot(hass)
    entries = roster_from_peers(hass, peers, registered_roster_entries(hass))
    source = roster_entry_for_target(str(call.data['source_automation']), entries)
    if source is None or not source.enabled or source.metadata.get('virtual_endpoint') != 'automation':
        raise ServiceValidationError("Select an enabled Automation contact as the calling identity")
    destination = str(call.data['destination']).strip()
    from .inbound_routing.targets import resolve_phone_target
    from .router import resolve_ha_router

    initial_route = resolve_ha_router(destination, entries)
    resolved = resolve_phone_target(
        decision=initial_route, endpoint_registry=runtime.endpoints,
        roster_entries=entries, route_resolver=resolve_ha_router,
    )
    if resolved.failure is not None:
        raise ServiceValidationError(f"Phone routing failed: {resolved.failure.decline_reason}")
    if resolved.decision != initial_route:
        destination = resolved.decision.target
    local_ip = await async_advertise_host(hass)
    resolver = EndpointRouteResolver(hass=hass, local_ip=local_ip, sip_port=int(runtime.transport_config['sip_port']))
    dialer = EndpointDialer(
        hass=hass, local_ip=local_ip, config=runtime.transport_config,
        route_resolver=resolver, sip_uri_transport=uri_transport,
        enable_reused_tcp_connection=enable_reused_tcp_connection,
    )
    def prepare(target):
        if dialer.browser_leg_for_member(target, peers, entries) is not None:
            raise ServiceValidationError("Outgoing automation calls currently require a SIP or ESPHome destination")
        uri, _, _ = dialer.sip_uri_for_member(target, peers, entries)
        if uri is None and target.lower().startswith(('sip:', 'sips:')):
            from .core.sip import parse_sip_uri
            uri = parse_sip_uri(target)
        if uri is None or resolver.is_local_listener_uri(uri) or is_trunk_uri(uri, runtime.trunk_config):
            raise ServiceValidationError("Select a reachable SIP account, ESPHome phone or direct SIP address")
        candidate = dialer.prepare_outbound_leg(
            member=target, peers=peers, roster_entries=entries,
            local_name=source.display_name, local_rtp_port_index=0,
            uri_override=str(uri),
            policy=OutboundLegPolicy(local_uri_user=source.extension or source.id, allow_video=False),
        )
        if candidate is None:
            raise ServiceValidationError("The destination cannot receive an outgoing automation call")
        return candidate

    attempt = prepare(destination)
    client = attempt.client
    call_id = client.dialog_ids.call_id
    session = None
    try:
        session = registry.upsert(
            call_id, state=CallState.CALLING.value, owner='automation',
            caller=source.display_name, callee=destination, route_kind='automation',
            origin='automation', source_automation=source.id,
            dest_endpoint_id=attempt.endpoint_id,
            policy_endpoint_ids=resolved.forwarded_endpoint_ids,
        )
        registry.own_resource(
            call_id, f'automation_ports:{call_id}', attempt.ports,
            lambda _reason, ports=attempt.ports: ports.release(), stage=CleanupStage.RESERVATION,
            generation=session.generation,
        )
        registry.attach_sip_client(call_id, call_id, client, role='callee', state=CallState.CALLING.value)
        if attempt.endpoint_id:
            registry.claim_endpoint(call_id, attempt.endpoint_id, role='destination')
        application = AutomationCall(
            hass, session, None, source, local_ip,
            controller=call.context.id, outgoing=True,
        )
        registry.own_resource(
            call_id, f'automation:{call_id}', application, application.close,
            stage=CleanupStage.MEDIA, generation=session.generation,
        )
        timeout = float(call.data.get('answer_timeout', 30))
        async with asyncio.timeout(timeout):
            while True:
                outcome, route = await _dial_attempt(hass, registry, session, attempt, destination, timeout)
                if outcome.disposition is not DialDisposition.REROUTE:
                    break
                session.ensure_live()
                if attempt.endpoint_id:
                    registry.release_endpoint_claim(call_id, attempt.endpoint_id)
                    session.update_metadata(policy_endpoint_ids=tuple(dict.fromkeys((
                        *session.metadata.get("policy_endpoint_ids", ()), attempt.endpoint_id,
                    ))))
                destination = route["destination"]
                rerouted = resolve_phone_target(
                    decision=resolve_ha_router(destination, entries), endpoint_registry=runtime.endpoints,
                    roster_entries=entries, route_resolver=resolve_ha_router,
                )
                if rerouted.failure is not None:
                    raise ServiceValidationError(f"Phone routing failed: {rerouted.failure.decline_reason}")
                destination = rerouted.decision.target or destination
                attempt = prepare(destination)
                client = attempt.client
                registry.own_resource(
                    call_id, f'automation_ports:{client.dialog_ids.call_id}', attempt.ports,
                    lambda _reason, ports=attempt.ports: ports.release(), stage=CleanupStage.RESERVATION,
                    generation=session.generation,
                )
                registry.transition(call_id, owner="automation", state=CallState.CALLING.value,
                                    callee=destination, dest_endpoint_id=attempt.endpoint_id)
                session.update_metadata(policy_endpoint_ids=tuple(dict.fromkeys((
                    *session.metadata.get("policy_endpoint_ids", ()), *rerouted.forwarded_endpoint_ids,
                ))))
                registry.add_leg(call_id, client.dialog_ids.call_id, role="callee", state="calling")
                registry.attach_sip_client(call_id, client.dialog_ids.call_id, client, role="callee", state="calling")
                if attempt.endpoint_id:
                    registry.claim_endpoint(call_id, attempt.endpoint_id, role="destination")
            if outcome.disposition is not DialDisposition.ANSWERED or client.dialog is None:
                raise ServiceValidationError(f"The destination did not answer: {outcome.reason or outcome.disposition.value}")
            application.require_current()
            # Final SIP acceptance ends ringing even if media preparation
            # needs another event-loop turn. TTS still waits for media.start().
            registry.transition(call_id, state=CallState.IN_CALL.value, expected_generation=session.generation)

            async def complete(reason):
                registry.request_termination(call_id, TerminationIntent(reason), generation=session.generation)

            media = LocalCallMedia(
                hass, invite=LocalAudioContract.from_dialog(client.dialog),
                local_rtp_port=attempt.ports.ports[0], reservation=attempt.ports,
                on_complete=complete,
            )
            if not media.can_send:
                raise ServiceValidationError("The destination cannot receive announcement audio")
            media.release_reservation_on_stop = False
            registry.attach_relay(call_id, media)
            application.media = media
            async def update_media(_current, updated, _method):
                commit = media.prepare_media_update(LocalAudioContract.from_dialog(updated))
                async def apply():
                    application.require_current()
                    commit()
                return apply
            client.on_media_update = update_media
            async def watch():
                try:
                    reason = await client.wait_for_dialog_termination()
                    await registry.terminate_call_wait(call_id, reason=reason)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await registry.terminate_call_wait(call_id, reason='signaling_failed')
            watcher = hass.async_create_task(watch())
            registry.attach_client_watcher(client.dialog_ids.call_id, watcher)
            application.require_current()
            # Teardown must cancel preparation before closing its media/ports.
            # Otherwise a remote BYE can leave start() waiting outside the owner.
            await session.create_task(media.start(), name="automation-media-start")
            application.require_current()
            if client.dialog is None:
                raise ServiceValidationError("The destination ended before audio became ready")
            application.answered = True


        application.arm_deadline()
        publish_bridge_projection(hass, session, event_type='answered', direction='outgoing', route_kind='automation')
        return {'call_id': call_id, 'generation': session.generation}
    except BaseException as err:
        if session is None:
            await async_close_outbound_leg(attempt, bye_or_cancel=True)
        else:
            await registry.terminate_call_wait(call_id, reason='automation_originate_failed')
        if isinstance(err, EndpointBusyError):
            raise ServiceValidationError("The destination is already in another call") from err
        if isinstance(err, TimeoutError):
            raise ServiceValidationError("The destination did not answer before the answer timeout") from err
        if isinstance(err, asyncio.CancelledError) and not asyncio.current_task().cancelling():
            raise ServiceValidationError("The destination ended while preparing call audio") from err
        raise
