"""The HA-Copilot agent: an LLM function-calling loop over the tool layer.

Talks to any OpenAI-compatible chat-completions endpoint (Ollama by default),
advertises the tool layer from :mod:`tools`, and iteratively executes tool calls
against the live Home Assistant instance until the model returns a final answer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_STEPS,
    CONF_MODEL,
    CONF_TEMPERATURE,
    DEFAULT_MAX_STEPS,
)
from . import tools

_LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are HA-Copilot, an AI deeply fused into this Home Assistant instance, "
    "like Cursor is fused into VS Code. You operate Home Assistant on the user's "
    "behalf using the provided tools: read and write config files, call services, "
    "inspect entity/area/device state, create automations, validate config, reload "
    "and read logs.\n"
    "Principles:\n"
    "- Prefer acting via tools over only describing steps. Take initiative.\n"
    "- When you decide to use a tool, EMIT THE TOOL CALL ITSELF. Never reply with\n"
    "  text like 'I will now call X' without actually calling it - just call it.\n"
    "- NEVER invent or guess entity_ids, service names, or areas. Discover the exact\n"
    "  entity_id with list_states (filter by domain, e.g. 'light') FIRST, then use\n"
    "  that exact full entity_id (e.g. 'light.living_room', including the domain).\n"
    "- call_service auto-resolves a friendly name or partial id to the real\n"
    "  entity_id when it is unambiguous (see the 'resolved' field), and otherwise\n"
    "  returns a 'candidates' list - pick one from it and retry; do NOT ask the\n"
    "  user to choose when exactly one candidate clearly matches their request.\n"
    "- After editing any YAML, call check_config, then reload the affected domain.\n"
    "- If a tool returns an error, read it and retry with corrected arguments.\n"
    "- Stop and give a final text answer only once the user's request is fully done.\n"
    "- Be concise. Report exactly what you changed and the verified result.\n"
    "- Answer the user in the same language they used."
)


async def run_agent(
    hass: HomeAssistant,
    store: dict,
    message: str,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """Run one agent turn. Returns {reply, steps, messages}."""
    session = async_get_clientsession(hass)
    base_url = store[CONF_BASE_URL].rstrip("/")
    model = store[CONF_MODEL]
    api_key = store.get(CONF_API_KEY) or "ollama"
    temperature = store.get(CONF_TEMPERATURE, 0.0)
    max_steps = store.get(CONF_MAX_STEPS, DEFAULT_MAX_STEPS)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    steps: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for _ in range(max_steps):
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools.TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": temperature,
            "stream": False,
        }
        try:
            async with session.post(
                f"{base_url}/chat/completions", json=payload, headers=headers, timeout=180
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {
                        "reply": f"LLM endpoint error {resp.status}: {text[:300]}",
                        "steps": steps,
                        "error": True,
                    }
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            return {
                "reply": (
                    f"Could not reach the LLM endpoint at {base_url}: {err}. "
                    "Check the base_url/model in your ha_copilot config (Ollama running?)."
                ),
                "steps": steps,
                "error": True,
            }

        choice = data["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []
        # Normalise assistant message back into the running transcript.
        messages.append(
            {
                "role": "assistant",
                "content": choice.get("content") or "",
                "tool_calls": tool_calls,
            }
        )

        if not tool_calls:
            return {"reply": choice.get("content") or "", "steps": steps, "messages": messages}

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            result = await tools.dispatch(hass, store, name, args)
            steps.append({"tool": name, "args": args, "result": result})
            _LOGGER.debug("ha_copilot tool %s(%s) -> %s", name, args, result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", name),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:6000],
                }
            )

    return {
        "reply": "Reached the maximum number of reasoning steps. Partial work may have been applied.",
        "steps": steps,
        "messages": messages,
    }
