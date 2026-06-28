"""Operate HA via the native MCP endpoint (/api/ha_copilot/mcp).

Usage: python op.py <scene_entity_id>
Calls scene.turn_on through the MCP call_service tool and prints a power snapshot.
"""
import sys
import time

import ha

def snap():
    tot = ha.get("/api/states/sensor.sonoff_total_power_usage")
    if not isinstance(tot, dict) or tot.get("state") in (None, "unknown", "unavailable"):
        # fallback: sum sonoff power
        tot = {"state": "?"}
    return tot.get("state")

def main():
    scene = sys.argv[1]
    ha.tool("call_service", domain="scene", service="turn_on", entity_id=scene)
    print(f"[MCP] scene.turn_on {scene} -> ok")
    time.sleep(4)
    print(f"     total power = {snap()} W")

if __name__ == "__main__":
    main()
