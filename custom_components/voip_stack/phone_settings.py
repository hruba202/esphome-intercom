"""Home Assistant boundary for persistent phone routing defaults."""

from dataclasses import replace

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .phone_config import update_phone_policy
from .phone_policy import DestinationKind, PhoneDestination
from .runtime_data import endpoint_directory
from .service_endpoints import async_require_phone_service_control
from .store import config_entry


async def async_set_phone_settings(call: ServiceCall) -> None:
    """Change future-call defaults without taking control of an active call."""
    registry = endpoint_directory(call.hass)
    endpoint = registry.by_device_id(call.data["device_id"])
    if endpoint is None:
        raise ServiceValidationError("Select an existing VoIP phone")
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack configuration is unavailable")
    updates = {}
    for field, slot in (
        ("forward_to", "always"),
        ("busy_to", "busy"),
        ("unavailable_to", "unavailable"),
    ):
        if field not in call.data:
            continue
        target = str(call.data[field]).strip()
        destination = registry.resolve(target) if target else None
        if destination is not None:
            if destination.endpoint_id == endpoint.endpoint_id:
                raise ServiceValidationError("A phone cannot forward to itself")
            target = destination.endpoint_id
        updates[slot] = PhoneDestination(DestinationKind.CONTACT, target) if target else None
    policy = replace(endpoint.policy, **updates)
    visited = {endpoint.endpoint_id}
    current = policy.always
    while current is not None and current.kind is DestinationKind.CONTACT:
        target = registry.resolve(current.target)
        if target is None:
            break
        if target.endpoint_id in visited:
            raise ServiceValidationError("The unconditional forwarding rules form a loop")
        visited.add(target.endpoint_id)
        current = target.policy.always
    update_phone_policy(call.hass, entry, endpoint, policy)
    # Use the established ESPHome API delivery path, including remote devices.
    from . import _refresh_and_push_phonebook

    await _refresh_and_push_phonebook(call.hass)
