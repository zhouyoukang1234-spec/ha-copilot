"""Verify the native LLM API exposes the full deterministic tool catalog.

Constructs :class:`custom_components.ha_copilot.llm_api.CopilotLLMAPI`, asks it
for an API instance, and checks that every :data:`tools.TOOL_SPECS` entry is
surfaced as a tool whose parameter schema round-trips losslessly through
``voluptuous_openapi.convert`` (the exact path a conversation agent uses). This
proves any HA conversation agent selecting "HA-Copilot" sees the same catalog
already published over HTTP and MCP. Run with the HA venv:

    python hactl/verify_llm_api.py
"""
import asyncio
import sys
from pathlib import Path

from voluptuous_openapi import convert

# Allow importing the custom component as a package from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ha_copilot import llm_api, tools  # noqa: E402


def main() -> int:
    api = llm_api.CopilotLLMAPI(
        hass=None, id=llm_api.LLM_API_ID, name=llm_api.LLM_API_NAME
    )
    instance = asyncio.run(api.async_get_api_instance(None))

    expected = len(tools.TOOL_SPECS)
    names = [tool.name for tool in instance.tools]
    duplicates = len(names) - len(set(names))

    lossless = 0
    bad: list[str] = []
    for tool, spec in zip(instance.tools, tools.TOOL_SPECS):
        want = spec["function"].get("parameters") or {
            "type": "object",
            "properties": {},
        }
        got = convert(tool.parameters, custom_serializer=instance.custom_serializer)
        if got == want:
            lossless += 1
        else:
            bad.append(tool.name)

    print(f"api: {api.id} / {api.name}")
    print(f"tools: {len(instance.tools)} (expected {expected})")
    print(f"duplicate names: {duplicates}")
    print(f"lossless schemas: {lossless}/{expected}")
    if bad:
        print(f"NON-LOSSLESS: {bad[:10]}")

    ok = (
        len(instance.tools) == expected
        and duplicates == 0
        and lossless == expected
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
