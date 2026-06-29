"""Diagnostic sensor entities for HA-Copilot.

Exposes operational metrics as native HA sensor entities so they appear in
the entity registry, can be graphed, and trigger automations. All sensors
are diagnostic (not user-facing controls) and update on demand.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .tools import TOOL_SPECS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HA-Copilot sensor entities from a config entry."""
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA-Copilot",
        manufacturer="HA-Copilot",
        model="Capability Layer",
        sw_version="0.6.0",
    )
    async_add_entities(
        [
            CopilotToolCountSensor(entry, device_info),
            CopilotDataSourcesSensor(entry, device_info),
            CopilotServiceCountSensor(entry, device_info),
        ]
    )


class CopilotToolCountSensor(SensorEntity):
    """Sensor showing the total number of available deterministic tools."""

    _attr_has_entity_name = True
    _attr_name = "Tool count"
    _attr_icon = "mdi:tools"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = "diagnostic"

    def __init__(
        self, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        self._attr_unique_id = f"{entry.entry_id}_tool_count"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int:
        """Return the current tool count."""
        return len(TOOL_SPECS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return tool names as attributes."""
        names = []
        for spec in TOOL_SPECS:
            func = spec.get("function", {})
            name = func.get("name", "")
            if name:
                names.append(name)
        return {"tools": names[:20], "total": len(TOOL_SPECS)}


class CopilotDataSourcesSensor(SensorEntity):
    """Sensor showing the number of free data sources available."""

    _attr_has_entity_name = True
    _attr_name = "Data sources"
    _attr_icon = "mdi:database-search"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = "diagnostic"

    _SOURCES = [
        "HACS (~2628 repos)",
        "GitHub search",
        "Community blueprints",
        "Zigbee devices (~2700)",
        "Z-Wave devices (~2375)",
        "Tasmota templates (~2800)",
        "ESPHome devices (~770)",
        "HA integrations (~1470)",
        "HA add-ons (78+)",
    ]

    def __init__(
        self, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        self._attr_unique_id = f"{entry.entry_id}_data_sources"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int:
        """Return the number of data sources."""
        return len(self._SOURCES)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return source list as attributes."""
        return {"sources": self._SOURCES}


class CopilotServiceCountSensor(SensorEntity):
    """Sensor showing the number of native HA services registered."""

    _attr_has_entity_name = True
    _attr_name = "Native services"
    _attr_icon = "mdi:cog-transfer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = "diagnostic"

    def __init__(
        self, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        self._attr_unique_id = f"{entry.entry_id}_service_count"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int:
        """Return total service count (run_tool + 12 resource services)."""
        return 14  # 13 resource services + run_tool

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return service names."""
        return {
            "services": [
                "run_tool",
                "discover_resources",
                "search_hacs",
                "search_github",
                "search_blueprints",
                "search_zigbee_devices",
                "search_zwave_devices",
                "search_tasmota_devices",
                "search_esphome_devices",
                "search_ha_integrations",
                "search_ha_addons",
                "recommend_resources",
                "recommend_blueprints",
            ],
            "routes": [
                "HA services",
                "MCP (JSON-RPC 2.0)",
                "Native LLM API",
                "HTTP",
                "WebSocket",
            ],
        }
