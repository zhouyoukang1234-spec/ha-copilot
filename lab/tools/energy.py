#!/usr/bin/env python3
"""能源域工具 (Energy Toolkit) — 把"功率(W) → 能耗(kWh) → 计量周期 → 能源面板"整条链路一次打通。

用户痛点：HA 能源面板只认 kWh 能量传感器(state_class total/total_increasing)，
但多数设备只暴露瞬时功率(W)。要上能源面板，用户得手动：
  1) 为每个功率源建 integration(黎曼积分)传感器 → 得 kWh
  2) 为每个 kWh 源建 utility_meter → 得日/月计量周期
  3) 进设置→能源，把这些源一个个加进面板
本工具把这三步编程化、一次成型。

    from energy import Energy
    e = Energy(config_dir, token)
    e.build_from_power(["sensor.sonoff_total_power_usage", ...])  # 写 integration+utility_meter 包
    # 重启一次让新域(utility_meter)初始化后：
    await e.set_energy_prefs(device_consumption=[...kWh entities...])
"""

from __future__ import annotations

import json
import os

import websockets
import yaml

ENERGY_PACKAGE = "packages/devin_energy.yaml"


def _energy_id(power_entity: str) -> str:
    base = power_entity.split(".", 1)[-1]
    for suf in ("_power", "_usage", "_w"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


class Energy:
    def __init__(self, config_dir: str, token: str,
                 ws_url: str = "ws://127.0.0.1:8123/api/websocket") -> None:
        self.config_dir = config_dir
        self.token = token
        self.ws_url = ws_url

    # ---- YAML: integration(Riemann) + utility_meter ----
    def build_from_power(self, power_entities: list[str], cycle: str = "daily") -> dict:
        """为每个功率源生成 kWh integration 传感器 + utility_meter 计量周期。"""
        path = os.path.join(self.config_dir, ENERGY_PACKAGE)
        doc = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle) or {}
        sensors = [s for s in (doc.get("sensor") or []) if s.get("platform") != "integration"]
        integ = []
        meters = doc.get("utility_meter") or {}
        energy_entities = []
        for pe in power_entities:
            base = _energy_id(pe)
            energy_name = f"devin_{base}_energy"
            integ.append({
                "platform": "integration", "source": pe, "name": energy_name,
                "unit_prefix": "k", "round": 3, "method": "trapezoidal",
            })
            energy_entities.append(f"sensor.{energy_name}")
            meters[f"devin_{base}_{cycle}"] = {
                "source": f"sensor.{energy_name}", "cycle": cycle,
            }
        doc["sensor"] = sensors + integ
        doc["utility_meter"] = meters
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)
        return {"energy_entities": energy_entities,
                "meters": list(meters.keys()), "path": path}

    # ---- WS: energy dashboard prefs ----
    async def _ws_call(self, payload: dict) -> dict:
        async with websockets.connect(self.ws_url, max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("WS auth failed")
            await ws.send(json.dumps({"id": 1, **payload}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    if not msg.get("success", True):
                        raise RuntimeError(f"{payload['type']} failed: {msg.get('error')}")
                    return msg.get("result")

    async def get_prefs(self) -> dict:
        return await self._ws_call({"type": "energy/get_prefs"})

    async def set_energy_prefs(self, device_consumption: list[str],
                               grid_consumption: list[str] | None = None) -> dict:
        """把能量传感器登记到能源面板：device_consumption=单设备能耗；grid_consumption=电网入。"""
        # HA 2025+ 统一格式：每个电网入量是独立的 grid 源对象(单连接)，
        # 不再用 legacy 的 flow_from/flow_to 数组。
        energy_sources = [{"type": "grid", "stat_energy_from": e,
                           "cost_adjustment_day": 0.0}
                          for e in (grid_consumption or [])]
        payload = {
            "type": "energy/save_prefs",
            "energy_sources": energy_sources,
            "device_consumption": [{"stat_consumption": e} for e in device_consumption],
        }
        return await self._ws_call(payload)
