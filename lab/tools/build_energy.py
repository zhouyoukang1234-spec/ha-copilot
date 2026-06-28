#!/usr/bin/env python3
"""一键打通能源域 — 轮次11 可复现脚本。

把一组功率(W)源 → integration(kWh) → utility_meter(日计量) → 能源面板，整条链路一次成型。

    python build_energy.py <HA_TOKEN> [--cycle daily] \
        --power sensor.a_power sensor.b_power [--grid sensor.x_energy]

注意：新增 integration/utility_meter 域后需重启一次 HA 让其初始化，之后再 set_energy_prefs。
本脚本默认假设这些域已就绪（写 YAML 后请重启再带 --prefs 运行）。
"""

from __future__ import annotations

import argparse
import asyncio

from energy import Energy


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--config", default="/home/ubuntu/ha_lab/config")
    ap.add_argument("--cycle", default="daily")
    ap.add_argument("--power", nargs="+", required=True)
    ap.add_argument("--grid", nargs="*", default=[])
    ap.add_argument("--prefs", action="store_true",
                    help="同时把生成的能量传感器登记到能源面板(需先重启加载新域)")
    args = ap.parse_args()

    e = Energy(args.config, args.token)
    res = e.build_from_power(args.power, cycle=args.cycle)
    print("写入 integration+utility_meter:", res["path"])
    for ent in res["energy_entities"]:
        print("  kWh:", ent)
    if args.prefs:
        grid = args.grid or res["energy_entities"][:1]
        device = [x for x in res["energy_entities"] if x not in grid]
        await e.set_energy_prefs(device_consumption=device, grid_consumption=grid)
        prefs = await e.get_prefs()
        print("能源面板源:", [s["type"] for s in prefs.get("energy_sources", [])],
              "| 设备能耗:", len(prefs.get("device_consumption", [])))


if __name__ == "__main__":
    asyncio.run(main())
