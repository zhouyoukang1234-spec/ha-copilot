"""Safety toggle switch entities for HA-Copilot.

Exposes ``allow_write`` and ``allow_restart`` as native HA switch entities
so they can be toggled from the dashboard, automations, or voice assistants.
Changes take effect immediately (modifying the in-memory store) and are
persisted back to the config entry options.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_STORE,
    DEFAULT_ALLOW_RESTART,
    DEFAULT_ALLOW_WRITE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HA-Copilot safety toggle switch entities."""
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA-Copilot",
        manufacturer="HA-Copilot",
        model="Capability Layer",
        sw_version="0.3.0",
    )
    async_add_entities(
        [
            CopilotSafetySwitch(
                hass, entry, device_info,
                conf_key=CONF_ALLOW_WRITE,
                name="Allow write",
                icon="mdi:pencil-lock",
                default=DEFAULT_ALLOW_WRITE,
            ),
            CopilotSafetySwitch(
                hass, entry, device_info,
                conf_key=CONF_ALLOW_RESTART,
                name="Allow restart",
                icon="mdi:restart-alert",
                default=DEFAULT_ALLOW_RESTART,
            ),
        ]
    )


class CopilotSafetySwitch(SwitchEntity):
    """A safety toggle for HA-Copilot capabilities."""

    _attr_has_entity_name = True
    _attr_entity_category = "config"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        *,
        conf_key: str,
        name: str,
        icon: str,
        default: bool,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._conf_key = conf_key
        self._default = default
        self._attr_unique_id = f"{entry.entry_id}_{conf_key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool:
        """Return true if the capability is enabled."""
        return self._entry.options.get(self._conf_key, self._default)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the capability."""
        await self._update(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the capability."""
        await self._update(False)

    async def _update(self, value: bool) -> None:
        """Apply the new value to both the store and config entry."""
        # Update the live in-memory store immediately.
        store = self.hass.data.get(DOMAIN, {}).get(DATA_STORE)
        if store is not None:
            store[self._conf_key] = value

        # Persist to config entry options.
        new_options = {**self._entry.options, self._conf_key: value}
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )
        self.async_write_ha_state()
        _LOGGER.info(
            "HA-Copilot %s set to %s", self._conf_key, value
        )
