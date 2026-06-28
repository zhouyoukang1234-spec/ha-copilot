"""Mock binary sensors for the Lab Sim twin."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimBinarySensor(eid) for eid in discovery_info["entities"])


class SimBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        self._attr_is_on = False
        low = entity_id.lower()
        if "motion" in low or "human" in low or "occupancy" in low:
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif "door" in low or "window" in low or "contact" in low:
            self._attr_device_class = BinarySensorDeviceClass.OPENING
