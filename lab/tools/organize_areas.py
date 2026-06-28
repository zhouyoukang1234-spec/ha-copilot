#!/usr/bin/env python3
"""按设备族把孪生里的实体批量归位到功能区域 — 轮次8 可复现脚本。

孪生里物理房间未知，故按"设备族 → 功能区域"归类（用户日后可改名为真实房间）。
一次运行把 300+ 实体从"无区域"整理到 12 个区域，替代用户在 UI 里逐个手点。

    python organize_areas.py <HA_TOKEN> [--ws ws://127.0.0.1:8123/api/websocket]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from registry import Registry

# (区域名, [匹配关键词])；按 entity_id/名称匹配，靠前优先。
RULES: list[tuple[str, list[str]]] = [
    ("储能区", ["river", "delta2", "ecoflow", "1838", "battery", "_kwh", "inv_out", "solar_w"]),
    ("天文台", ["_sun_", "sun_solar", "solar_midnight", "solar_noon", "sun2",
              "rising", "setting", "dawn", "dusk"]),
    ("安防", ["camera", "lock", "occupancy", "motion", "human", "door", "_door", "security"]),
    ("影音", ["media_player", "speaker", "tts", "lx06", "lx04", "l16a", "x08e",
            "xiaoai", "audio", "volume"]),
    ("照明", ["light.", "led", "_deng", "deng_", "lamp", "bulb", "brightness"]),
    ("开关插座", ["sonoff", "chuangmi", "plug", "socket", "switch.", "_switch"]),
    ("环境感知", ["temperature", "humidity", "miaomiaoce", "miaomiaoc", "pm25",
              "pm2_5", "co2", "_temp", "illuminance"]),
    ("风扇空调", ["fan.", "_fan", "climate", "air_condition"]),
    ("系统运维", ["backup", "watchman", "version", "_update", "feedparser", "average", "deploy"]),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--ws", default="ws://127.0.0.1:8123/api/websocket")
    args = ap.parse_args()
    async with Registry(args.token, args.ws) as reg:
        stats = await reg.assign_by_rules(RULES, only_unassigned=True)
    total = sum(v for k, v in stats.items() if not k.startswith("_"))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"归位 {total} 个实体；未匹配 {stats['_unmatched']}（脚本/自动化/helper 不属区域）；"
          f"已有区域跳过 {stats['_skipped']}")


if __name__ == "__main__":
    asyncio.run(main())
