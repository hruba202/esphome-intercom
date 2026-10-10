"""Session-owned no-answer defaults driven by existing HA call events."""

from __future__ import annotations

import logging

from homeassistant.core import callback

from .automation_routing import automation_event_type, matches_call
from .call_deadlines import CallDeadline
from .endpoint_session import TerminationIntent
from .inbound_routing.automation import SIP_ROUTE_DECISION_TIMEOUT
from .phone_policy import DestinationKind
from .runtime_data import registration_data, runtime_data
from .websocket_api import CALL_EVENT


_LOGGER = logging.getLogger(__name__)
_TIMER = "deadline:phone_policy"
_DECISION = "deadline:phone_policy_decision"
_RINGING = {"ringing", "remote_ringing"}


def setup_phone_policy_events(hass, entry) -> None:
    """Observe call events without adding polling or a second call registry."""

    @callback
    def receive(event):
        apply_phone_policy_event(hass, dict(event.data))

    entry.async_on_unload(hass.bus.async_listen(CALL_EVENT, receive))


def apply_phone_policy_event(hass, payload: dict) -> None:
    runtime = runtime_data(hass)
    registry = runtime.sip if runtime is not None else None
    if registry is None:
        return
    session = registry.get_session(str(payload.get("call_id") or ""))
    if session is None or not session.live or session.generation != payload.get("generation"):
        return
    if payload.get("scope") == "phone_policy":
        return
    if session.state not in _RINGING or automation_event_type(payload) == "forwarding":
        for name in (_TIMER, _DECISION):
            deadline = registry.resource_for(session.call_id, name)
            if deadline is not None:
                deadline.cancel()
        session.metadata.pop("phone_policy_ringing_target", None)
        return
    if session.route_kind in {"group", "ring", "ring_group", "conference"}:
        return
    endpoint_id = str(session.metadata.get("dest_endpoint_id")
                      or session.metadata.get("target_endpoint_id") or "")
    endpoint = runtime.endpoints.get(endpoint_id) if endpoint_id else runtime.endpoints.resolve(session.callee)
    if endpoint is None:
        return
    target = (endpoint.endpoint_id, session.callee)
    if session.metadata.get("phone_policy_ringing_target") == target:
        return
    for name in (_TIMER, _DECISION):
        deadline = registry.resource_for(session.call_id, name)
        if deadline is not None:
            deadline.cancel()
    # Snapshot even an unset rule, so a later settings edit cannot affect a
    # call that was already ringing before that edit.
    session.metadata["phone_policy_ringing_target"] = target
    policy = endpoint.policy
    if policy.ringing_timeout is None:
        return
    token, owner, callee = session.token, session.owner, session.callee

    def current():
        candidate = registry.get_session(token.call_id, generation=token.generation)
        if (candidate is not session or not session.live or session.owner != owner
                or session.state not in _RINGING or session.callee != callee):
            return False
        return True

    async def execute(sequence):
        context = registry.event_context(token.call_id)
        if not current() or context is None or context.sequence != sequence:
            return
        destination = policy.no_answer
        if destination.kind is DestinationKind.HANGUP:
            registry.request_termination(
                token.call_id, TerminationIntent("no_answer", public_state="idle", response_status=480),
                generation=token.generation,
            )
            return
        target_endpoint = runtime.endpoints.get(destination.target)
        target_name = target_endpoint.name if target_endpoint is not None else destination.target
        if destination.kind is DestinationKind.VOICEMAIL:
            target_name = f"voicemail.{destination.target}"
        try:
            if registry.forward_call is None:
                raise RuntimeError("SIP forwarding is unavailable")
            await registry.forward_call(
                call_id=token.call_id, destination=target_name, on_failure="terminate",
                expected_state=session.state, expected_sequence=sequence,
            )
        except Exception:
            _LOGGER.exception("Phone no-answer route failed call_id=%s", token.call_id)
            context = registry.event_context(token.call_id)
            if current() and context is not None and context.sequence == sequence:
                registry.request_termination(token.call_id, TerminationIntent("route_failed"), generation=token.generation)

    def commit(sequence):
        if current():
            task = session.create_task(execute(sequence), name="phone-policy-fallback")
            # Session.create_task is the existing task/cleanup owner.
            return task
        return None

    def expired():
        if not current():
            return
        context = registry.event_context(token.call_id)
        if context is None:
            return
        sequence = context.sequence
        event = {**payload, "scope": "phone_policy", "event_type": "ringing_timeout_requested",
                 "reason": "no_answer", "sequence": sequence, "timeout": policy.ringing_timeout}
        interested = any(
            matches_call(event, options)
            and (options.get("for") is None or options["for"].total_seconds() <= policy.ringing_timeout)
            for options in registration_data(hass).unanswered_trigger_filters.values()
        )
        hass.bus.async_fire(CALL_EVENT, event, context=registry.ha_context(token.call_id))
        if interested:
            decision = CallDeadline(registry, session, f"{_DECISION}:{session.call_id}", lambda: commit(sequence))
            decision.arm(hass.loop, SIP_ROUTE_DECISION_TIMEOUT)
        else:
            commit(sequence)

    deadline = CallDeadline(registry, session, f"{_TIMER}:{session.call_id}", expired)
    deadline.arm(hass.loop, policy.ringing_timeout)
