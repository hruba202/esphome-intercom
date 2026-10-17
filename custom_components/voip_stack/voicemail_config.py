"""One global IVR voice configuration, independent of phone transport."""

from homeassistant.exceptions import ServiceValidationError

from .store import config_entry

IVR_LANGUAGES = frozenset({"en", "it", "de", "pt"})


def voice_engine(hass, identity):
    from homeassistant.components.tts.const import DATA_COMPONENT, DATA_TTS_MANAGER
    from homeassistant.components.tts.helper import get_engine_instance

    if DATA_COMPONENT not in hass.data or DATA_TTS_MANAGER not in hass.data:
        return None
    return get_engine_instance(hass, identity)


def prompt_languages(engine):
    return [language for language in engine.supported_languages or ()
            if language.replace("_", "-").split("-", 1)[0].lower() in IVR_LANGUAGES]


def voicemail_config(hass):
    entry = config_entry(hass)
    config = dict(entry.data.get("voicemail", {})) if entry is not None else {}
    config.pop("enabled", None)
    return config


def mailbox_routes_available(hass):
    """Keep preexisting user routes authoritative when feature codes collide."""
    from .config_validation import entry_route_mappings, mailbox_namespace_conflicts
    entry = config_entry(hass)
    return entry is not None and not mailbox_namespace_conflicts(
        entry_route_mappings(entry, entry.data, include_features=False))


def require_voicemail_config(hass):
    config = voicemail_config(hass)
    if not config.get("tts_entity_id") or not config.get("language"):
        raise ServiceValidationError("Configure the voicemail TTS voice and language in VoIP Stack settings")
    entity = voice_engine(hass, config["tts_entity_id"])
    if entity is None or not entity.available:
        raise ServiceValidationError("The configured voicemail TTS provider is unavailable")
    if not mailbox_routes_available(hass):
        raise ServiceValidationError("Voicemail feature codes conflict with an existing phone, contact or group")
    return config
