"""Config flow for HA-Copilot.

Adds UI-based setup (Settings → Integrations → Add Integration) and an
options flow so users can toggle ``allow_write`` / ``allow_restart`` from
the integration card without touching YAML.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DEFAULT_ALLOW_RESTART,
    DEFAULT_ALLOW_WRITE,
    DOMAIN,
)


class HACopilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA-Copilot."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — single-instance guard + safety toggles."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="HA-Copilot",
                data={},
                options={
                    CONF_ALLOW_WRITE: user_input.get(
                        CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE
                    ),
                    CONF_ALLOW_RESTART: user_input.get(
                        CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALLOW_WRITE, default=DEFAULT_ALLOW_WRITE
                    ): bool,
                    vol.Optional(
                        CONF_ALLOW_RESTART, default=DEFAULT_ALLOW_RESTART
                    ): bool,
                }
            ),
            description_placeholders={
                "name": "HA-Copilot",
            },
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> HACopilotOptionsFlow:
        """Return the options flow handler."""
        return HACopilotOptionsFlow(config_entry)


class HACopilotOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle HA-Copilot options (safety toggles)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALLOW_WRITE,
                        default=current.get(
                            CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_ALLOW_RESTART,
                        default=current.get(
                            CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART
                        ),
                    ): bool,
                }
            ),
        )
