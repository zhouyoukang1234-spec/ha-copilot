"""Mock selects for the Lab Sim twin."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize

_DEFAULT_OPTIONS = ["自动", "手动", "关闭"]


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimSelect(eid) for eid in discovery_info["entities"])


class SimSelect(SelectEntity):
    _attr_should_poll = False
    _attr_options = _DEFAULT_OPTIONS

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        self._attr_current_option = _DEFAULT_OPTIONS[0]

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
