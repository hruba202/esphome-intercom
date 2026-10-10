"""Logical target resolution and pre-transport route validation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from ..endpoint_session import EndpointCallSession
from ..fsm import TerminalReason
from ..phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    PhoneEndpoint,
)
from ..router import RouteAction, RouteReason
from ..phone_policy import DestinationKind
from ..sip_listener import SipInviteResult

if TYPE_CHECKING:
    from ..endpoint_registry import EndpointRegistry
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TargetResolution:
    """Resolved dialplan decision, endpoint and optional terminal result."""

    decision: RouteDecision
    endpoint: PhoneEndpoint | None
    failure: SipInviteResult | None = None
    forwarded_endpoint_ids: tuple[str, ...] = ()


def _decision_endpoint(
    endpoint_registry: EndpointRegistry | None,
    decision: RouteDecision,
) -> PhoneEndpoint | None:
    if endpoint_registry is None or decision.entry is None:
        return None
    endpoint_id = str((decision.entry.metadata or {}).get("endpoint_id") or "").strip()
    return endpoint_registry.get(endpoint_id) if endpoint_id else None


def resolve_phone_target(
    *,
    decision: RouteDecision,
    endpoint_registry: EndpointRegistry | None,
    roster_entries: list[Any],
    route_resolver: Callable[..., RouteDecision],
    call_id: str = "",
    apply_policy: bool = True,
) -> TargetResolution:
    """Resolve phone defaults before creating any transport or browser leg.

    Explicit automation decisions can bypass defaults. Permissions still belong
    to the caller and must be checked against the final route by the caller.
    """

    visited_endpoint_ids: set[str] = set()
    while True:
        candidate = _decision_endpoint(endpoint_registry, decision)
        if candidate is None or not apply_policy:
            return TargetResolution(decision, candidate, forwarded_endpoint_ids=tuple(sorted(visited_endpoint_ids)))
        if decision.reason is RouteReason.TARGET_DISABLED:
            return TargetResolution(decision, candidate, forwarded_endpoint_ids=tuple(sorted(visited_endpoint_ids)))
        fallback = candidate.policy.before_ringing(
            busy=bool(candidate.dnd or (candidate.active_call_id and candidate.active_call_id != call_id)),
            unavailable=candidate.availability is not EndpointAvailability.AVAILABLE,
        )
        forward_target = ""
        if fallback is not None:
            if fallback.kind is DestinationKind.HANGUP:
                return TargetResolution(decision, candidate, SipInviteResult(
                    486, "Busy Here", to_tag="", decline_reason="phone_policy",
                ))
            if fallback.kind is DestinationKind.VOICEMAIL:
                # Do not reinterpret an unavailable application as a trunk or
                # silently dial the mailbox owner's ordinary telephone.
                return TargetResolution(decision, candidate, SipInviteResult(
                    480, "Temporarily Unavailable", to_tag="",
                    decline_reason="voicemail_unavailable",
                ))
            forward_target = fallback.target
        if not forward_target:
            return TargetResolution(decision, candidate, forwarded_endpoint_ids=tuple(sorted(visited_endpoint_ids)))
        if candidate.endpoint_id in visited_endpoint_ids:
            _LOGGER.warning(
                "Phone forward loop rejected call_id=%s endpoint=%s visited=%s",
                call_id,
                candidate.endpoint_id,
                sorted(visited_endpoint_ids),
            )
            return TargetResolution(
                decision,
                candidate,
                SipInviteResult(
                    480,
                    "Temporarily Unavailable",
                    to_tag="",
                    decline_reason="forward_loop",
                ),
            )
        visited_endpoint_ids.add(candidate.endpoint_id)
        # Persisted endpoint references survive display-name/extension changes.
        # Resolve them to the current public alias before using the dial plan.
        target_endpoint = endpoint_registry.get(forward_target)
        if target_endpoint is not None:
            forward_target = target_endpoint.name
        decision = route_resolver(forward_target, roster_entries)
        _LOGGER.info(
            "Phone forward call_id=%s endpoint=%s destination=%s route=%s",
            call_id,
            candidate.endpoint_id,
            forward_target,
            decision.action.value,
        )


def resolve_inbound_target(*, invite: SipInvite, **kwargs) -> TargetResolution:
    """Adapt an incoming SIP request to the common phone-policy resolver."""
    return resolve_phone_target(call_id=invite.call_id, **kwargs)


def validate_target_endpoint(
    *,
    invite: SipInvite,
    endpoint: PhoneEndpoint | None,
) -> SipInviteResult | None:
    """Reject DND, busy, disabled and offline physical endpoints."""

    if endpoint is None:
        return None
    if endpoint.dnd:
        _LOGGER.info(
            "SIP INVITE rejected by endpoint DND call_id=%s endpoint=%s",
            invite.call_id,
            endpoint.endpoint_id,
        )
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason="dnd",
        )
    if endpoint.active_call_id and endpoint.active_call_id != invite.call_id:
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason=TerminalReason.BUSY.value,
        )
    if endpoint.availability is EndpointAvailability.UNAVAILABLE:
        return SipInviteResult(
            480,
            "Temporarily Unavailable",
            to_tag="",
            decline_reason=RouteReason.TARGET_DISABLED.value,
        )
    if (
        endpoint.availability is EndpointAvailability.OFFLINE
        and not endpoint.kind.is_softphone
    ):
        # Registrar devices persist while offline, but a missing Contact
        # cannot receive a standards-based SIP dialog.
        return SipInviteResult(
            480,
            "Temporarily Unavailable",
            to_tag="",
            decline_reason=RouteReason.TARGET_UNREACHABLE.value,
        )
    # A browser card is a media attachment, not the logical phone. An offline
    # browser remains ringable for automations and missed-call handling.
    return None


def reject_route_decision(
    *,
    hass: HomeAssistant,
    invite: SipInvite,
    decision: RouteDecision,
    session: EndpointCallSession,
) -> SipInviteResult | None:
    """Publish and return a terminal response for a rejected dialplan route."""

    if decision.action is not RouteAction.REJECT:
        return None
    if decision.reason is RouteReason.TARGET_DISABLED:
        status = 403
        sip_reason = "Forbidden"
    elif decision.reason in {
        RouteReason.TRUNK_UNAVAILABLE,
        RouteReason.TARGET_UNREACHABLE,
    }:
        status = 480
        sip_reason = "Temporarily Unavailable"
    else:
        status = 404
        sip_reason = "Not Found"
    terminal_reason = (
        decision.reason.value if decision.reason else TerminalReason.DECLINED.value
    )
    return SipInviteResult(
        status,
        sip_reason,
        to_tag="",
        decline_reason=terminal_reason,
    )
