"""Mock text entities for the Lab Sim twin.

Covers e.g. Xiaomi 小爱音箱 `text.*_execute_text_directive` — settable free-text
entities the user's automations/scripts write voice directives into.
"""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimText(eid) for eid in discovery_info["entities"])


class SimText(TextEntity):
    _attr_should_poll = False
    _attr_native_min = 0
    _attr_native_max = 255

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        self._attr_native_value = ""

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
