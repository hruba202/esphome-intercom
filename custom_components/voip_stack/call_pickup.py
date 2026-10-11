"""Directed pickup joins an identified phone to an existing ringing call."""

import asyncio
from dataclasses import dataclass, replace
import secrets

from homeassistant.exceptions import ServiceValidationError

from .endpoint_session import CleanupStage, TerminationIntent
from .call_scope import has_ringing_destination
from .runtime_data import require_runtime_data, sip_endpoint_manager
from .sip_listener import SipInviteResult


@dataclass(slots=True)
class PickupClaim:
    endpoint_id: str
    claimed: bool = False


@dataclass(slots=True)
class RetiredForward:
    endpoint_id: str
    task: object


def ringing_target(hass, target):
    runtime = require_runtime_data(hass)
    endpoint = runtime.endpoints.resolve(target)
    session = (
        runtime.sip.get_session(endpoint.active_call_id)
        if endpoint and endpoint.active_call_id
        else None
    )
    if (
        session is None
        or not session.live
        or not has_ringing_destination(session, endpoint.endpoint_id)
        or session.owner not in {"bridge", "ha_softphone", "local_bridge", "router"}
        or endpoint.endpoint_id == session.metadata.get("source_endpoint_id")
        or endpoint.endpoint_id
        not in {
            session.metadata.get("dest_endpoint_id"),
            session.metadata.get("target_endpoint_id"),
            session.metadata.get("endpoint_id"),
        }
    ):
        raise ServiceValidationError(
            "This phone has no ringing call available for pickup"
        )
    return runtime, endpoint, session


async def pickup_browser(call, endpoint, target):
    from homeassistant.core import ServiceCall
    from .softphone_answer import async_answer_browser_call
    from .softphone_commands import async_resolve_browser_call_command

    runtime, target_endpoint, session = ringing_target(call.hass, target)
    if endpoint.endpoint_id in session.endpoint_claims or endpoint.active_call_id:
        raise ServiceValidationError("Choose an idle phone to pick up another phone")
    if runtime.sip.forward_call is None:
        raise ServiceValidationError("Call forwarding is unavailable")
    await runtime.sip.forward_call(
        call_id=session.call_id,
        destination=endpoint.name,
        on_failure="resume",
        expected_state=session.state,
    )
    task = runtime.sip.task_for(session.call_id, "forward")
    if task is not None:
        await task
    if not session.live or session.owner not in {"ha_softphone", "local_bridge"}:
        raise ServiceValidationError("The ringing call changed before pickup")
    answer = ServiceCall(
        call.hass,
        "voip_stack",
        "answer",
        {
            "device_id": endpoint.device_id,
            "call_id": session.call_id,
            "media_client_id": str(call.data.get("media_client_id") or ""),
        },
        context=call.context,
    )
    command = await async_resolve_browser_call_command(
        call.hass, answer, endpoint_id=endpoint.endpoint_id, endpoint=endpoint
    )
    await async_answer_browser_call(call.hass, answer, command)


async def prepare_sip_pickup(hass, incoming, target, local_ip):
    from .core.sdp import build_answer_directional, first_offered_dtmf_format
    from .core.rtp import AudioRtpSenderState
    from .call_projection import publish_bridge_projection
    from .dtmf_events import attach_dtmf_event_bridge
    from .inbound_answer import async_commit_runtime_answer
    from .local_softphone_runtime import PendingLocalSource, local_softphone_bridge
    from .media_ports import RtpPortReservation
    from .pending_sip_destination import PendingSipDestination
    from .sip_bridge import invite_rtp_peer, local_rtp_peer
    from .local_media_bridge import LocalMediaBridge, application_rtp_peer
    from .sip_rtp_bridge import SipRtpRelay, _RelayProtocol
    from .sip_runtime import send_final_response
    from .websocket_api import _release_ha_softphone_claim

    picker = incoming.identified_source_endpoint_id
    if not picker:
        return SipInviteResult(403, "Forbidden")
    try:
        runtime, target_endpoint, session = ringing_target(hass, target)
    except ServiceValidationError:
        return SipInviteResult(480, "Temporarily Unavailable")
    registry = runtime.sip
    if (
        picker in session.endpoint_claims
        or registry.resource_for(session.call_id, "pickup") is not None
    ):
        return SipInviteResult(486, "Busy Here")
    source = session.artifacts.pending_invite or session.artifacts.caller_invite
    try:
        native = PendingSipDestination.capture(hass, registry, session)
    except ServiceValidationError:
        return SipInviteResult(480, "Temporarily Unavailable")
    local_source = registry.resource_for(session.call_id, "browser_source")
    application = registry.resource_for(
        session.call_id, "automation"
    ) or registry.resource_for(session.call_id, "parking")
    if native is not None:
        source = native.invite
    if session.owner == "local_bridge":
        bridge = local_softphone_bridge(hass)
        local_source = PendingLocalSource(
            hass,
            bridge.get_call(session.call_id),
            local_ip,
            int(runtime.transport_config["sip_port"]),
        )
        source = local_source.invite
    elif (
        source is None
        and session.owner == "ha_softphone"
        and session.state == "remote_ringing"
    ):
        local_source = PendingLocalSource(
            hass,
            None,
            local_ip,
            int(runtime.transport_config["sip_port"]),
            call_id=session.call_id,
        )
        source = local_source.invite
    if (
        source is None
        or source.video_format is not None
        or incoming.video_format is not None
    ):
        return SipInviteResult(488, "Not Acceptable Here")
    claim = PickupClaim(picker)
    resource_name = f"pickup:{session.call_id}"
    reservation = None
    relay = None
    media_handoff = None
    acquired = False
    committed = False
    changed = False
    expected_owner = session.owner
    expected_state = session.state
    preanswered = registry.resource_for(session.call_id, "preanswered") or {}
    from .local_call_media import ApplicationCallSource

    source_answered = bool(
        isinstance(source, ApplicationCallSource)
        or preanswered.get("final_response_sent")
        or source.initial_response_sent
    )
    source_ports = (
        preanswered.get("rtp_reservation")
        if source_answered and local_source is None
        else None
    )
    if source_answered and local_source is None and source_ports is None:
        source_ports = next(
            (
                resource.value
                for resource in session.resources
                if resource.name.startswith("pending_bridge_media:")
            ),
            None,
        )
        if source_ports is None:
            return SipInviteResult(488, "Not Acceptable Here")
    previous_forward = (
        registry.task_for(session.call_id, "forward")
        if session.owner == "router"
        else None
    )
    previous_destination = registry.bridge_link_for(session.call_id)

    def validate():
        if (
            not session.live
            or session.owner != expected_owner
            or session.state != expected_state
            or registry.resource_for(session.call_id, "pickup") is not claim
        ):
            raise ServiceValidationError("The call was answered or its route changed")
        if native is not None:
            native.validate()
        previous_client = (
            registry.sip_client_for(previous_destination)
            if previous_destination
            else None
        )
        if previous_client is not None and previous_client.dialog is not None:
            raise ServiceValidationError("The original phone already answered")
        if (
            local_source is not None
            and local_source.outgoing
            and not local_source.retired
            and local_source.previous_client.dialog is not None
        ):
            raise ServiceValidationError("The original phone already answered")

    async def rollback():
        if committed:
            return
        if relay is not None:
            await relay.stop()
        if reservation is not None:
            reservation.release()
        if session.live:
            registry.release_resource(session.call_id, resource_name, value=claim)
            if acquired:
                registry.release_endpoint_claim(session.call_id, picker)
            if changed:
                await registry.terminate_call_wait(
                    session.call_id, reason="pickup_failed"
                )

    registry.own_resource(
        session.call_id,
        resource_name,
        claim,
        lambda _reason: rollback(),
        stage=CleanupStage.MEDIA,
        generation=session.generation,
    )
    try:
        registry.claim_endpoint(
            session.call_id, picker, role="destination", adopt_transport=True
        )
        acquired = True
        reservation = RtpPortReservation.allocate(hass)
        left = (
            local_rtp_peer("127.0.0.1", source.recv_format, source.send_format)
            if local_source is not None
            else application_rtp_peer(source)
        )
        right = invite_rtp_peer(incoming)
        directions = (
            left.can_send,
            left.can_receive,
            right.can_send,
            right.can_receive,
        )
        relay = SipRtpRelay(
            left=replace(left, can_send=False, can_receive=False),
            right=replace(right, can_send=False, can_receive=False),
            left_port=source_ports.ports[0]
            if source_ports is not None
            else reservation.ports[0],
            right_port=reservation.ports[1],
        )
        if source_ports is not None:
            sock = relay._rtp_socket(relay.right_port)
            try:
                relay.right_transport, _ = await hass.loop.create_datagram_endpoint(
                    lambda: _RelayProtocol(relay, "right"), sock=sock
                )
            except BaseException:
                sock.close()
                raise
        else:
            await relay.start()
        if (
            source_ports is not None
            and application is not None
            and application.media is not None
        ):
            media = application.media
            media_handoff = LocalMediaBridge(
                media, media.invite, relay, media.transport
            )
            media_handoff.validate()
        validate()
        answer = build_answer_directional(
            local_ip,
            local_ip,
            relay.right_port,
            incoming.send_format,
            incoming.recv_format,
            dtmf=first_offered_dtmf_format(incoming.remote_sdp),
            remote_sdp=incoming.remote_sdp,
        )
    except asyncio.CancelledError:
        await rollback()
        if not session.live:
            return SipInviteResult(480, "Temporarily Unavailable")
        raise
    except (ServiceValidationError, ValueError, RuntimeError, OSError) as error:
        await rollback()
        return SipInviteResult(
            503 if isinstance(error, OSError) else 480, "Temporarily Unavailable"
        )
    except BaseException:
        await rollback()
        raise

    async def commit():
        nonlocal committed, changed
        validate()
        claim.claimed = True
        registry.transition(
            session.call_id,
            owner="router",
            state="connecting",
            expected_generation=session.generation,
        )
        changed = True
        manager = sip_endpoint_manager(hass)
        registry.attach_inbound_dialog(
            session.call_id,
            incoming.call_id,
            manager,
            endpoint_id=picker,
            expected_generation=session.generation,
        )
        session.update_metadata(dest_endpoint_id=picker, target_endpoint_id=picker)
        if previous_forward is not None:
            registry.cancel_task(session.call_id, "forward")
            retired = RetiredForward(target_endpoint.endpoint_id, previous_forward)
            name = f"retired_destination:forward:{incoming.call_id}"

            async def drain(_reason):
                await asyncio.gather(previous_forward, return_exceptions=True)

            resource = session.add_resource(
                name, retired, drain, stage=CleanupStage.LEG
            )

            async def finish_old_route():
                await resource.close("picked_up")
                if session.live:
                    registry.release_resource(session.call_id, name, value=retired)

            session.create_task(finish_old_route(), name="pickup-retired-forward")
        if native is not None:
            await native.retire()
        elif local_source is not None:
            await local_source.retire_destination()
            local_source.pause()
            local_source.commit()
        else:
            if target_endpoint.kind.is_softphone:
                _release_ha_softphone_claim(
                    hass,
                    session.call_id,
                    endpoint_id=target_endpoint.endpoint_id,
                    destination=incoming.caller,
                )
        if media_handoff is not None:
            media_handoff.commit()
        if application is not None:
            await application.release_media_for_forward()
        if not session.live:
            raise ServiceValidationError("The caller ended during pickup")
        if relay.left_transport is None:
            sock = relay._rtp_socket(relay.left_port)
            try:
                relay.left_transport, _ = await hass.loop.create_datagram_endpoint(
                    lambda: _RelayProtocol(relay, "left"), sock=sock
                )
            except BaseException:
                sock.close()
                raise
        audio_source = preanswered.get("audio_rtp_source")
        if isinstance(audio_source, AudioRtpSenderState):
            relay.left.sequence, relay.left.timestamp, relay.left.ssrc = (
                audio_source.sequence,
                audio_source.timestamp,
                audio_source.ssrc,
            )
        if source_ports is not None:
            current_source = (
                registry.resource_for(session.call_id, "preanswered") or {}
            ).get("rtp_reservation") or source_ports
            merged = RtpPortReservation.merge(
                hass,
                (current_source.ports, reservation.ports),
                (relay.left_port, relay.right_port),
            )
            current_source.detach()
            reservation.detach()
            relay._on_release = lambda _ports: merged.release()
        else:
            relay._on_release = lambda _ports: reservation.release()
        registry.attach_relay(session.call_id, relay)
        registry.take_media(session.call_id, provisional=True)
        if local_source is not None:
            registry.own_resource(
                session.call_id, f"browser_source:{session.call_id}", local_source, None
            )
            registry.attach_media(
                session.call_id,
                {
                    "rtp_loopback": True,
                    "remote_rtp_host": "127.0.0.1",
                    "remote_rtp_port": relay.left_port,
                    "send_format": source.recv_format,
                    "recv_format": source.send_format,
                    "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                    "endpoint_id": local_source.call.caller_endpoint_id,
                },
            )
        source_answer = (
            build_answer_directional(
                local_ip,
                local_ip,
                relay.left_port,
                source.send_format,
                source.recv_format,
                dtmf=first_offered_dtmf_format(source.remote_sdp),
                remote_sdp=source.remote_sdp,
            )
            if local_source is None and not source_answered
            else ""
        )
        result = await async_commit_runtime_answer(
            registry,
            session.call_id,
            source_answer,
            send_final_response=send_final_response,
            response_context=hass,
            owner="bridge",
            caller=session.caller,
            callee=incoming.caller,
            route_kind="pickup",
            source_endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
            dest_endpoint_id=picker,
            response_already_sent=source_answered or local_source is not None,
        )
        if not result.committed:
            raise ServiceValidationError(
                "The caller ended before pickup could complete"
            )
        registry.set_bridge_link(session.call_id, incoming.call_id)
        session.update_metadata(
            target_endpoint_id=picker, connected_party=incoming.caller
        )
        (
            relay.left.can_send,
            relay.left.can_receive,
            relay.right.can_send,
            relay.right.can_receive,
        ) = directions
        attach_dtmf_event_bridge(
            hass,
            relay,
            call_id=session.call_id,
            dest_call_id=incoming.call_id,
            caller=session.caller,
            callee=incoming.caller,
            source_client=application.source_client
            if application is not None
            else None,
        )
        if application is not None and application.source_client is not None:
            from .bridge_media_updates import BridgeMediaUpdateBinder

            BridgeMediaUpdateBinder(hass).attach_audio_side(
                application.source_client,
                relay,
                source_call_id=session.call_id,
                side="left",
            )
        committed = True
        registry.release_resource(session.call_id, resource_name, value=claim)
        if local_source is not None:
            local_source.connected(incoming.caller, "pickup")
        if application is not None:
            await application.close("picked_up")
            registry.release_resource(
                session.call_id,
                f"{application.owner}:{session.call_id}",
                value=application,
            )
        publish_bridge_projection(
            hass, session, event_type="answered", route_kind="pickup"
        )

    return SipInviteResult(
        200,
        "OK",
        answer_sdp=answer,
        commit=commit,
        rollback=rollback,
        connected_identity_name=session.caller,
        connected_identity_user=source.routing_caller,
    )
