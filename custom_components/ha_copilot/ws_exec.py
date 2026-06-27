"""Execute any Home Assistant WebSocket command in-process.

The Home Assistant frontend drives almost everything (registries, Lovelace
dashboards, automation/scene/script config, config entries, auth, system
health, ...) through the WebSocket API. Those command handlers are registered
in ``hass.data[websocket_api.const.DOMAIN]`` as ``{command: (handler, schema)}``
and are normally invoked by a live socket connection.

This module synthesises a minimal in-process :class:`ActiveConnection`, captures
the single result/error the handler emits, and returns it -- giving the native
co-pilot the *entire* WebSocket surface without any network round-trip. One
mechanism, total reach: 无为而无不为.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any

from homeassistant.auth.models import User
from homeassistant.components.websocket_api import const as ws_const
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class WSCommandError(Exception):
    """Raised when an in-process WebSocket command fails."""


def _coerce_message(raw: Any) -> dict[str, Any]:
    """Normalise whatever ``send_message`` was handed into a dict."""
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw)
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    raise WSCommandError(f"unexpected ws message type: {type(raw).__name__}")


async def async_ws_execute(
    hass: HomeAssistant,
    user: User,
    command_type: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    """Run a single WebSocket command and return its ``result``.

    ``user`` is the authenticated HA user the command runs as, so the real
    permission model applies. Raises :class:`WSCommandError` on failure.
    """
    handlers: dict = hass.data.get(ws_const.DOMAIN) or {}
    entry = handlers.get(command_type)
    if entry is None:
        raise WSCommandError(f"unknown websocket command '{command_type}'")
    handler, schema = entry

    msg: dict[str, Any] = {"id": 1, "type": command_type, **(payload or {})}
    if schema is not None and schema is not False:
        try:
            msg = schema(msg)
        except Exception as err:  # noqa: BLE001 - surface validation to caller
            raise WSCommandError(f"invalid arguments for '{command_type}': {err}") from err

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _send(raw: Any) -> None:
        if future.done():
            return
        try:
            data = _coerce_message(raw)
        except Exception as err:  # noqa: BLE001
            future.set_exception(err)
            return
        # Ignore event/subscription frames; we only want the command's reply.
        if data.get("type") == "event":
            return
        future.set_result(data)

    # ActiveConnection's signature drifts across HA versions (e.g. 'remote' was
    # removed and 'refresh_token' became required in 2026.x). Build kwargs from
    # the actual signature so this works across versions.
    _ac_params = inspect.signature(ActiveConnection.__init__).parameters
    _ac_kwargs: dict[str, Any] = {}
    if "refresh_token" in _ac_params:
        # 2026.x stores ``refresh_token.id`` in __init__, so it can't be None.
        # Reuse one of the user's real tokens; fall back to a minimal stand-in
        # (only ``.id`` is dereferenced) so in-process exec never needs auth.
        rt = next(iter(getattr(user, "refresh_tokens", {}).values()), None)
        if rt is None:
            rt = SimpleNamespace(id="ha_copilot_inproc")
        _ac_kwargs["refresh_token"] = rt
    if "remote" in _ac_params:
        _ac_kwargs["remote"] = None
    connection = ActiveConnection(_LOGGER, hass, _send, user, **_ac_kwargs)

    result = handler(hass, connection, msg)
    if asyncio.iscoroutine(result):
        await result

    data = await asyncio.wait_for(future, timeout=timeout)
    if not data.get("success", False):
        raise WSCommandError(
            f"ws '{command_type}' failed: {data.get('error') or data}"
        )
    return data.get("result")
