"""Immutable call preconditions shared by PBX operations and HA adapters."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .endpoint_session import EndpointCallSession


@dataclass(frozen=True, slots=True)
class LegSnapshot:
    leg_id: str
    endpoint_id: str
    sip_call_id: str
    role: str
    state: str
    phase: str


@dataclass(frozen=True, slots=True)
class CallStateSnapshot:
    """Immutable result of a committed mutation, never a second call owner."""

    call_id: str
    generation: int
    owner: str
    state: str
    phase: str
    caller: str
    callee: str
    route_kind: str
    legs: tuple[LegSnapshot, ...]
    participants: tuple[str, ...]
    topology: tuple[tuple[str, str], ...]
    media_contracts: tuple[tuple[str, tuple[str, ...]], ...]
    revision: int = field(compare=False)
    sequence: int = field(compare=False)

    @classmethod
    def capture(cls, session: "EndpointCallSession", media_contracts=()) -> "CallStateSnapshot":
        return cls(
            session.call_id, session.generation, session.owner, session.state,
            session.phase.value, session.caller, session.callee, session.route_kind,
            tuple(LegSnapshot(leg.leg_id, leg.endpoint_id, leg.sip_call_id,
                              leg.role, leg.state, leg.phase.value)
                  for leg in session.legs.values()),
            tuple(sorted(session.endpoint_claims)),
            tuple((key, str(session.metadata.get(key) or "")) for key in (
                "source_endpoint_id", "dest_endpoint_id", "target_endpoint_id",
                "endpoint_id", "connected_party", "bridge_dest_call_id",
            )),
            tuple(media_contracts),
            session.revision, session.event_context.sequence,
        )


@dataclass(frozen=True, slots=True)
class CallPrecondition:
    """One accepted operation refers to this owner and revision, not a UI event."""

    call_id: str
    generation: int
    owner: str
    sequence: int

    @classmethod
    def capture(cls, session: "EndpointCallSession") -> "CallPrecondition":
        return cls(session.call_id, session.generation, session.owner,
                   session.event_context.sequence)

    def matches(self, session: "EndpointCallSession | None") -> bool:
        return bool(session is not None and session.live
                    and session.call_id == self.call_id
                    and session.generation == self.generation
                    and session.owner == self.owner
                    and session.event_context.sequence == self.sequence)


def validate_call_snapshot(registry, call_id, *, expected_state="",
                           expected_sequence=0, precondition=None, endpoint_id=""):
    """Translate legacy event guards once, retaining strict operation ownership."""
    from .service_errors import service_error

    session = registry.get_session(registry.resolve_session_id(call_id))
    context = registry.event_context(call_id)
    if precondition is not None and not precondition.matches(session):
        raise service_error("The call changed during the operation",
                            "call_changed_during_operation", call_id=call_id)
    actual = context.state if context is not None else "ended"
    if endpoint_id and session is not None:
        leg = session.legs.get(f"browser:{endpoint_id}")
        if leg is None:
            matching = [item for item in session.legs.values() if item.endpoint_id == endpoint_id]
            leg = matching[0] if len(matching) == 1 else None
        if leg is not None:
            actual = leg.state
        elif endpoint_id not in session.endpoint_claims:
            actual = "not_participant"
    expected = str(expected_state or "").strip().lower()
    # These are the two endpoint perspectives of the same unanswered phase.
    ringing = {"ringing", "remote_ringing"}
    if expected and actual != expected and not {actual, expected} <= ringing:
        raise service_error(f"call_id {call_id} is {actual}, expected {expected}",
                            "call_state_mismatch", call_id=call_id,
                            actual=actual, expected=expected)
    sequence = context.sequence if context is not None else 0
    if expected_sequence and sequence != int(expected_sequence):
        raise service_error(
            f"call_id {call_id} sequence is {sequence}, expected {expected_sequence}",
            "call_sequence_mismatch", call_id=call_id,
            actual=sequence, expected=expected_sequence,
        )
    return context
