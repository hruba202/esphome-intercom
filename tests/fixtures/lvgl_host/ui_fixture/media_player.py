"""Expose the real MediaPlayer interface to a host UI fixture."""
import esphome.codegen as cg
from esphome.components import media_player
import esphome.config_validation as cv
from esphome.const import CONF_ID

from . import ui_fixture_ns

UiPlayer = ui_fixture_ns.class_("UiPlayer", media_player.MediaPlayer, cg.Component)
CONFIG_SCHEMA = media_player.media_player_schema(UiPlayer).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await media_player.register_media_player(var, config)
