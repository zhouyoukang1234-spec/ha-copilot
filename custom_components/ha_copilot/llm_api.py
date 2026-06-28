"""Expose HA-Copilot's deterministic tool layer through Home Assistant's
native LLM API (:mod:`homeassistant.helpers.llm`).

Registering here means *any* conversation agent configured in Home Assistant
(OpenAI, Anthropic, Google, Ollama / local models, the built-in Assist, ...)
can select **HA-Copilot** as its "control" API and immediately gains the full
catalog of 119 deterministic tools — without each provider needing our custom
HTTP / MCP endpoints. The same :data:`tools.TOOL_SPECS` remains the single
source of truth shared by the ``run_tool`` service, the HTTP surface, the MCP
server, and this native API, so the four routes never drift.

This module bundles **no model** and performs **no inference**; it only wires
the existing deterministic dispatcher into the native tool-calling framework.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from voluptuous_openapi import UNSUPPORTED

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps
from homeassistant.util.json import JsonObjectType

from . import tools
from .const import DATA_STORE, DOMAIN

try:
    # Home Assistant's own AssistAPI uses this helper to list the entities the
    # user has exposed to a given assistant (respecting their expose settings),
    # resolving area names via the registries. We reuse it verbatim so the
    # context we hand the model matches HA's native behaviour exactly.
    from homeassistant.helpers.llm import (  # type: ignore[attr-defined]
        _get_exposed_entities,
    )
except ImportError:  # pragma: no cover - internal helper renamed/moved
    _get_exposed_entities = None

LLM_API_ID = "ha_copilot"
LLM_API_NAME = "HA-Copilot (deterministic tools)"

API_PROMPT = (
    "You are operating Home Assistant through HA-Copilot, a deterministic "
    "capability layer. Each tool maps one-to-one onto a concrete Home Assistant "
    "operation (read state, call services, query the registries, inspect "
    "config, manage automations/dashboards, etc.) and returns structured JSON. "
    "Prefer these tools over guessing: call a read tool to confirm the current "
    "state before acting, then call the matching write tool. Tool arguments and "
    "results are exact — no tool invents data or runs a model."
)


class _RawSchema:
    """Marker carrying a raw JSON Schema for a tool's parameters.

    ``voluptuous_openapi.convert`` calls the API instance's custom serializer on
    the parameters object (anything that is not a :class:`voluptuous.Schema` is
    passed straight through); returning the stored JSON Schema verbatim lets a
    conversation agent see exactly the spec already published over HTTP and MCP.
    Validation of arguments is performed by the deterministic dispatcher itself,
    so this object is intentionally not a validating schema.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema_json = schema


def _custom_serializer(schema: Any) -> Any:
    """Pass HA-Copilot tool parameter schemas through unchanged."""
    if isinstance(schema, _RawSchema):
        return copy.deepcopy(schema.schema_json)
    return UNSUPPORTED


def _coerce_json(result: Any) -> JsonObjectType:
    """Coerce a tool result into plain JSON types (matching the MCP route).

    Tools may return registry objects, ``datetime`` values, ``Context``, etc.
    Round-tripping through Home Assistant's JSON encoder yields the same
    serialisation the HTTP/MCP surfaces already return.
    """
    plain = json.loads(json_dumps(result))
    if isinstance(plain, dict):
        return plain
    return {"result": plain}


class _CopilotTool(llm.Tool):
    """A single HA-Copilot deterministic tool, presented to the LLM framework."""

    def __init__(self, spec: dict[str, Any]) -> None:
        function = spec.get("function") or {}
        self.name = function.get("name", "")
        self.description = function.get("description")
        parameters = function.get("parameters") or {
            "type": "object",
            "properties": {},
        }
        # Carry the raw JSON Schema; _custom_serializer emits it unchanged.
        self.parameters = _RawSchema(parameters)

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        store = hass.data[DOMAIN][DATA_STORE]
        result = await tools.dispatch(
            hass, store, self.name, tool_input.tool_args or {}
        )
        return _coerce_json(result)


def _exposed_entities_prompt(exposed: dict[str, dict[str, Any]]) -> str:
    """Render exposed entities as a compact, area-grouped context block.

    Borrowed from HA's AssistAPI pattern: giving the model the entity_ids it may
    operate (grouped by area, with name + current state) up front makes tool
    calls more precise and saves the round-trips/tokens of discovery calls.
    """
    by_area: dict[str, list[str]] = {}
    for entity_id, info in exposed.items():
        area = info.get("areas") or "Unassigned"
        name = info.get("names") or entity_id
        state = info.get("state", "")
        by_area.setdefault(area, []).append(f"- {entity_id} ({name}) = {state}")

    lines = [
        f"Currently exposed Home Assistant entities ({len(exposed)} total), "
        "grouped by area. Operate them by entity_id; prefer an exact match here "
        "over guessing names or calling a discovery tool first:"
    ]
    for area in sorted(by_area):
        lines.append(f"\n[{area}]")
        lines.extend(sorted(by_area[area]))
    return "\n".join(lines)


def exposed_entities(
    hass: HomeAssistant, assistant: str = "conversation"
) -> dict[str, Any]:
    """Raw exposed-entity map ``{entity_id: {areas, names, state}}``.

    The structured counterpart to :func:`entity_context_block`, used by the MCP
    ``resources`` capability to enumerate addressable per-entity resources.
    Returns an empty dict when nothing is exposed or the HA helper is missing.
    """
    if not assistant or _get_exposed_entities is None:
        return {}
    return _get_exposed_entities(hass, assistant) or {}


def entity_context_block(
    hass: HomeAssistant, assistant: str = "conversation"
) -> str:
    """Area-grouped context block for entities exposed to ``assistant``.

    Shared by the native LLM API and the MCP ``prompts`` capability so both
    routes hand a client the *same* AssistAPI-style context. ``assistant``
    defaults to ``"conversation"`` (the built-in Assist conversation agent),
    which is the assistant MCP clients have no way to name themselves. Returns
    an empty string when nothing is exposed or the HA helper is unavailable.
    """
    if not assistant or _get_exposed_entities is None:
        return ""
    exposed = _get_exposed_entities(hass, assistant)
    if not exposed:
        return ""
    return _exposed_entities_prompt(exposed)


class CopilotLLMAPI(llm.API):
    """Native LLM API backed by the HA-Copilot deterministic tool layer."""

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        api_prompt = API_PROMPT
        assistant = llm_context.assistant if llm_context else None
        if assistant:
            block = entity_context_block(self.hass, assistant)
            if block:
                api_prompt = f"{API_PROMPT}\n\n{block}"
        return llm.APIInstance(
            api=self,
            api_prompt=api_prompt,
            llm_context=llm_context,
            tools=[_CopilotTool(spec) for spec in tools.TOOL_SPECS],
            custom_serializer=_custom_serializer,
        )


def async_register_llm_api(hass: HomeAssistant) -> None:
    """Register HA-Copilot as a native LLM API (idempotent across reloads)."""
    try:
        llm.async_register_api(
            hass, CopilotLLMAPI(hass=hass, id=LLM_API_ID, name=LLM_API_NAME)
        )
    except HomeAssistantError:
        # Already registered (e.g. a previous setup in this process) — fine.
        pass
