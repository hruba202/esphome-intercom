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
    return dict(entry.data.get("voicemail", {})) if entry is not None else {}


def require_voicemail_config(hass):
    config = voicemail_config(hass)
    if not config.get("enabled") or not config.get("tts_entity_id") or not config.get("language"):
        raise ServiceValidationError("Enable voicemail and choose its TTS voice and language in VoIP Stack settings")
    return config
