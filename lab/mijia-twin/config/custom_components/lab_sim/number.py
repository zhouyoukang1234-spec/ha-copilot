"""Mock numbers for the Lab Sim twin."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimNumber(eid) for eid in discovery_info["entities"])


class SimNumber(NumberEntity):
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        self._attr_native_value = 0

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
