"""RFC 4235 full-state documents derived from the existing call registry."""

from dataclasses import dataclass
import re
from xml.etree import ElementTree

from .core import sip


@dataclass(frozen=True, slots=True)
class DialogState:
    identity: str
    state: str
    direction: str
    call_id: str = ""
    local_tag: str = ""
    remote_tag: str = ""
    held: bool = False


def subscription_filter(event_header):
    """Interpret the RFC 4235 dialog identifiers, preserving their case."""
    _package, pairs = sip._split_semicolon_params(event_header)
    values = {}
    for name, value in pairs:
        if name not in {"call-id", "to-tag", "from-tag"}:
            continue
        if name in values or not value:
            raise ValueError("invalid dialog subscription filter")
        if value.startswith('"') and value.endswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        values[name] = value
    if values and (not values.get("call-id") or not values.get("to-tag")):
        raise ValueError("dialog filter requires call-id and to-tag")
    return tuple(values.get(key, "") for key in ("call-id", "to-tag", "from-tag"))


def snapshot(runtime, endpoint_id, match=("", "", "")):
    calls = runtime.sip
    if calls is None:
        return ()
    manager = calls.component("udp_listener")
    result = []
    for session in tuple(calls.sessions.values()):
        if endpoint_id not in session.endpoint_claims:
            continue
        source = endpoint_id == session.metadata.get("source_endpoint_id")
        leg = next((item for item in session.legs.values() if item.endpoint_id == endpoint_id), None)
        wire_id = (leg.sip_call_id if leg is not None else
                   session.call_id if source else str(session.metadata.get("bridge_dest_call_id") or ""))
        client = calls.sip_client_for(wire_id)
        key = getattr(client, "dialog_ids", None)
        if key is None and manager is not None:
            key = manager.dialog_key(wire_id)
        # The document describes the monitored phone, opposite HA's local
        # dialog perspective. Do not invent SIP identifiers for browser legs.
        call_id = wire_id if key is not None else ""
        local_tag = str(getattr(key, "remote_tag", ""))
        remote_tag = str(getattr(key, "local_tag", ""))
        if any(expected and actual != expected for actual, expected in zip(
            (call_id, local_tag, remote_tag), match, strict=True,
        )):
            continue
        state = leg.state if leg is not None else session.state
        phase = {"new": "trying", "route_requested": "trying", "calling": "trying",
                 "connecting": "proceeding", "ringing": "early", "remote_ringing": "early",
                 "in_call": "confirmed", "answered": "confirmed", "held": "confirmed",
                 "transferring": "confirmed"}.get(state, "terminated")
        result.append(DialogState(
            f"{session.call_id}:{session.generation}", phase,
            "initiator" if source else "recipient", call_id, local_tag, remote_tag,
            held=state == "held",
        ))
    return tuple(result)


def document(entity, version, states):
    root = ElementTree.Element("dialog-info", {
        "xmlns": "urn:ietf:params:xml:ns:dialog-info", "version": str(version),
        "state": "full", "entity": entity,
    })
    for item in states:
        attributes = {"id": item.identity, "direction": item.direction}
        for key, value in (("call-id", item.call_id), ("local-tag", item.local_tag), ("remote-tag", item.remote_tag)):
            if value:
                attributes[key] = value
        dialog = ElementTree.SubElement(root, "dialog", attributes)
        ElementTree.SubElement(dialog, "state").text = item.state
        if item.held:
            local = ElementTree.SubElement(dialog, "local")
            target = ElementTree.SubElement(local, "target", {"uri": entity})
            ElementTree.SubElement(target, "param", {"pname": "+sip.rendering", "pvalue": "no"})
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
