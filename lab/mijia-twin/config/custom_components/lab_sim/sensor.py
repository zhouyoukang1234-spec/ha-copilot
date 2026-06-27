"""Mock sensors for the Lab Sim twin."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import guess_sensor, humanize


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimSensor(eid) for eid in discovery_info["entities"])


class SimSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)
        state, unit, device_class = guess_sensor(entity_id)
        self._attr_native_value = state
        self._attr_native_unit_of_measurement = unit
        if device_class in ("temperature", "humidity", "battery", "power",
                            "energy", "voltage", "current", "duration",
                            "data_size"):
            self._attr_device_class = device_class
