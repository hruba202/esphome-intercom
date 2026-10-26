"""Resolve shared entities by their stable Home Assistant registry identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

from .const import DOMAIN


def shared_entity_ids(hass: HomeAssistant) -> dict[str, str | None]:
    """Return current IDs, including user renames, without adopting namesakes."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    return {
        "call_event": registry.async_get_entity_id(
            "event", DOMAIN, "voip_stack_call_event"
        ),
        "phonebook": registry.async_get_entity_id(
            "sensor", DOMAIN, "voip_stack_phonebook"
        ),
    }


def phonebook_state(hass: HomeAssistant) -> State | None:
    """Return the registered phonebook state, if its entity is loaded."""
    entity_id = shared_entity_ids(hass)["phonebook"]
    return hass.states.get(entity_id) if entity_id else None
