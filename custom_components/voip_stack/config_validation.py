"""Pure validation helpers shared by the VoIP Stack config flow and tests."""

from collections.abc import Iterable, Mapping
from typing import Any

from .const import CONF_PHONEBOOK_CONTACTS
from .roster import normalize_roster_key
from .mailbox_address import parse_mailbox_address
from .parking_config import parse_parking_address


ROUTE_ALIAS_FIELDS = ("id", "name", "extension", "number", "username")
GROUP_ALIAS_FIELDS = ("ring_group", "conference_group")


def normalized_aliases(values: Iterable[object]) -> set[str]:
    """Return non-empty aliases normalized exactly like the SIP router."""
    return {
        key
        for value in values
        if (key := normalize_roster_key(str(value or "")))
    }


def mailbox_route_aliases(item: Mapping[str, Any]) -> tuple[str, ...]:
    """Named mailbox routes share the phone/contact namespace when enabled."""
    settings = item.get("voicemail")
    if not isinstance(settings, Mapping) or not settings.get("enabled", False):
        return ()
    aliases = tuple(f"{prefix}.{value}" for prefix in ("voicemail", "mailbox")
                 for field in ("endpoint_id", "id", "name", "extension", "username")
                 if (value := str(item.get(field) or "").strip()))
    extension = str(item.get("extension") or "")
    return (*aliases, f"*60{extension}") if extension.isdigit() else aliases


def mailbox_namespace_conflicts(mappings):
    """Reject existing user routes that enabling mailbox codes would shadow."""
    return any(parse_mailbox_address(value) is not None
               for item in mappings
               for value in (*(item.get(field) for field in ROUTE_ALIAS_FIELDS), *mapping_group_values(item)))


def mapping_route_aliases(item: Mapping[str, Any]) -> set[str]:
    """Return the ordinary phone/contact aliases of one persisted mapping."""
    return normalized_aliases(item.get(field) for field in ROUTE_ALIAS_FIELDS)


def mapping_group_values(item: Mapping[str, Any]) -> list[str]:
    """Return group routes declared by one mapping (comma-separated allowed)."""
    values: list[str] = []
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for field in GROUP_ALIAS_FIELDS:
        raw = item.get(field, metadata.get(field))
        values.extend(part.strip() for part in str(raw or "").split(","))
    return values


def mapping_group_aliases(item: Mapping[str, Any]) -> set[str]:
    return normalized_aliases(mapping_group_values(item))


def route_namespace_conflicts(
    *,
    candidate_routes: Iterable[object] = (),
    candidate_mailbox_routes: Iterable[object] = (),
    candidate_groups: Iterable[object] = (),
    existing: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """Check routes and group names as one canonical routing namespace.

    Reusing a group name on multiple members is intentional. A phone/contact
    alias may not shadow a group, and a newly declared group may not shadow an
    existing phone/contact alias.
    """
    raw_routes = {str(value or "").strip().casefold() for value in candidate_routes} - {""}
    raw_groups = {str(value or "").strip().casefold() for value in candidate_groups} - {""}
    mailbox_routes = {str(value).strip().casefold() for value in candidate_mailbox_routes}
    wanted_routes = normalized_aliases(raw_routes)
    wanted_groups = normalized_aliases(raw_groups)
    if wanted_routes & wanted_groups or mailbox_routes & (raw_routes | raw_groups):
        return True
    existing_routes: set[str] = set()
    existing_groups: set[str] = set()
    existing_mailboxes: set[str] = set()
    existing_exact: set[str] = set()
    for item in existing:
        if not isinstance(item, Mapping):
            continue
        if item.get("parking_feature_routes") and any(
            parse_parking_address(item["parking_feature_routes"], value) is not None for value in raw_routes | raw_groups
        ):
            return True
        if item.get("mailbox_feature_routes") and any(
            parse_mailbox_address(value) is not None for value in raw_routes | raw_groups
        ):
            return True
        existing_routes.update(mapping_route_aliases(item))
        existing_groups.update(mapping_group_aliases(item))
        existing_mailboxes.update(value.casefold() for value in mailbox_route_aliases(item))
        existing_exact.update(str(value or "").strip().casefold()
                              for value in (*(item.get(field) for field in ROUTE_ALIAS_FIELDS), *mapping_group_values(item)))
    return bool(
        wanted_routes & (existing_routes | existing_groups)
        or wanted_groups & existing_routes
        or mailbox_routes & (existing_exact | existing_mailboxes)
        or (raw_routes | raw_groups) & existing_mailboxes
    )


def extension_conflicts(extension: str, existing: Mapping[str, Any]) -> bool:
    """Return whether an extension collides with a persisted route."""
    wanted = normalize_roster_key(str(extension))
    for key in ("sip_accounts", CONF_PHONEBOOK_CONTACTS):
        for item in existing.get(key, []) or []:
            if not isinstance(item, Mapping):
                continue
            values = (item.get("extension"), item.get("number"), item.get("username"))
            if wanted in normalized_aliases(values):
                return True
    return False
