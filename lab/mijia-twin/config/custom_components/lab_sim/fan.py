"""Mock fans for the Lab Sim twin."""

from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimFan(eid) for eid in discovery_info["entities"])


class SimFan(FanEntity):
    _attr_should_poll = False
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        self._attr_is_on = False
        self._attr_percentage = 0

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs) -> None:
        self._attr_is_on = True
        if percentage is not None:
            self._attr_percentage = percentage
        elif not self._attr_percentage:
            self._attr_percentage = 100
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._attr_percentage = 0
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        self._attr_percentage = percentage
        self._attr_is_on = percentage > 0
        self.async_write_ha_state()
