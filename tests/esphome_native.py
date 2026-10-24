"""Locate the installed official ESPHome providers for integration contracts."""
from pathlib import Path

import esphome


NATIVE_COMPONENTS = Path(esphome.__file__).resolve().parent / "components"
