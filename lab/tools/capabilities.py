#!/usr/bin/env python3
"""统一能力层 (Capability Layer) — 把所有 lab 工具归一成一套带 schema 的可调用能力。

这是"代替用户操作一切"的融合落点：把分散的 registry / author / energy / dashgen /
backup / templating / blueprint / deploy_integration 收敛为**单一调度接口**，
每个能力带 JSON schema（与 MCP tools/list 同构），可被对话式 / MCP / HTTP 统一调用。
后续把本表直接搬进 ha_copilot 组件的 tools.py + MCP 服务即完成上提。

    import asyncio
    from capabilities import list_capabilities, dispatch
    print(list_capabilities())                       # MCP tools/list 形态
    asyncio.run(dispatch("area_list", {}, token=TOKEN, config_dir=CFG))

能力 handler 统一签名：async (args: dict, *, token, config_dir, base_url) -> result
（同步工具在 handler 内直接调用，无需 await。）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import author
import backup as backup_mod
import blueprint as blueprint_mod
import dashgen
import deploy_integration
import energy as energy_mod
import registry as registry_mod
import templating as templating_mod

Handler = Callable[..., Awaitable[Any]]
_REGISTRY: dict[str, dict] = {}


def capability(name: str, description: str, params: dict) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _REGISTRY[name] = {"name": name, "description": description,
                           "parameters": {"type": "object", "properties": params},
                           "handler": fn}
        return fn
    return deco


def list_capabilities() -> list[dict]:
    """返回所有能力的 schema（去掉 handler），即 MCP tools/list 形态。"""
    return [{k: v for k, v in c.items() if k != "handler"} for c in _REGISTRY.values()]


async def dispatch(name: str, args: dict, *, token: str, config_dir: str,
                   base_url: str = "http://127.0.0.1:8123") -> Any:
    if name not in _REGISTRY:
        raise KeyError(f"unknown capability: {name}")
    return await _REGISTRY[name]["handler"](
        args or {}, token=token, config_dir=config_dir, base_url=base_url)


# ----------------- 注册表 / 区域 -----------------
@capability("area_list", "列出所有区域", {})
async def _area_list(args, *, token, config_dir, base_url):
    async with registry_mod.Registry(token) as r:
        return await r.areas()


@capability("area_assign_by_rules",
            "按关键词规则把未归区实体批量归位到区域",
            {"rules": {"type": "array", "description": "[[区域名,[关键词...]]...]"},
             "only_unassigned": {"type": "boolean"}})
async def _area_assign(args, *, token, config_dir, base_url):
    rules = [(r[0], r[1]) for r in args["rules"]]
    async with registry_mod.Registry(token) as r:
        return await r.assign_by_rules(rules, args.get("only_unassigned", True))


# ----------------- 构造器 (自动化/场景/脚本/helper) -----------------
def _author(config_dir, token, base_url):
    return author.Authoring(config_dir, base_url=base_url, token=token)


@capability("create_automation", "创建/替换一个自动化并热重载",
            {"alias": {"type": "string"}, "trigger": {"type": "array"},
             "action": {"type": "array"}, "condition": {"type": "array"}})
async def _create_automation(args, *, token, config_dir, base_url):
    a = _author(config_dir, token, base_url)
    e = a.create_automation(args["alias"], args["trigger"], args["action"],
                            args.get("condition"))
    a.reload_automations()
    return e


@capability("create_blueprint_automation", "从蓝图实例化自动化并热重载",
            {"alias": {"type": "string"}, "blueprint_path": {"type": "string"},
             "inputs": {"type": "object"}})
async def _create_bp_auto(args, *, token, config_dir, base_url):
    a = _author(config_dir, token, base_url)
    e = a.create_blueprint_automation(args["alias"], args["blueprint_path"], args["inputs"])
    a.reload_automations()
    return e


@capability("create_scene", "创建/替换场景(实体状态快照)并热重载",
            {"name": {"type": "string"}, "entities": {"type": "object"}})
async def _create_scene(args, *, token, config_dir, base_url):
    a = _author(config_dir, token, base_url)
    e = a.create_scene(args["name"], args["entities"])
    a.reload("scene")
    return e


@capability("create_script", "创建/替换脚本(动作序列)并热重载",
            {"alias": {"type": "string"}, "sequence": {"type": "array"}})
async def _create_script(args, *, token, config_dir, base_url):
    a = _author(config_dir, token, base_url)
    e = a.create_script(args["alias"], args["sequence"])
    a.reload("script")
    return e


@capability("create_helper", "定义 helper 实体(input_*/timer/counter)",
            {"domain": {"type": "string"}, "object_id": {"type": "string"},
             "config": {"type": "object"}})
async def _create_helper(args, *, token, config_dir, base_url):
    a = _author(config_dir, token, base_url)
    return a.create_helper(args["domain"], args["object_id"], args["config"])


# ----------------- 模板 -----------------
@capability("template_render", "对实时状态渲染/校验 Jinja 模板",
            {"template": {"type": "string"}})
async def _tpl_render(args, *, token, config_dir, base_url):
    t = templating_mod.Templating(config_dir, token, base_url)
    ok, msg = t.validate(args["template"])
    return {"ok": ok, "result": msg}


@capability("create_template_sensor", "先校验后部署模板传感器并热重载",
            {"name": {"type": "string"}, "state": {"type": "string"},
             "unit": {"type": "string"}, "device_class": {"type": "string"},
             "icon": {"type": "string"}})
async def _tpl_sensor(args, *, token, config_dir, base_url):
    t = templating_mod.Templating(config_dir, token, base_url)
    e = t.create_sensor(args["name"], args["state"], unit=args.get("unit"),
                        device_class=args.get("device_class"), icon=args.get("icon"))
    t.reload()
    return e


# ----------------- 能源 -----------------
@capability("energy_build_from_power",
            "为功率源生成 integration(kWh)+utility_meter 计量",
            {"power_entities": {"type": "array"}, "cycle": {"type": "string"}})
async def _energy_build(args, *, token, config_dir, base_url):
    e = energy_mod.Energy(config_dir, token)
    return e.build_from_power(args["power_entities"], args.get("cycle", "daily"))


@capability("energy_set_prefs", "把能量传感器登记进能源面板",
            {"device_consumption": {"type": "array"}, "grid_consumption": {"type": "array"}})
async def _energy_prefs(args, *, token, config_dir, base_url):
    e = energy_mod.Energy(config_dir, token)
    await e.set_energy_prefs(args["device_consumption"], args.get("grid_consumption"))
    return await e.get_prefs()


# ----------------- 仪表盘 -----------------
@capability("dashboard_generate_areas",
            "读注册表自动生成按区域分视图的 Lovelace 仪表盘", {})
async def _dash(args, *, token, config_dir, base_url):
    return await dashgen.build(token, config_dir)


# ----------------- 备份 -----------------
@capability("backup_create", "创建备份(等待完成)",
            {"name": {"type": "string"}})
async def _bk_create(args, *, token, config_dir, base_url):
    async with backup_mod.Backup(token) as b:
        return await b.wait_for(await b.create(args.get("name", "Devin 快照")))


@capability("backup_list", "列出备份", {})
async def _bk_list(args, *, token, config_dir, base_url):
    async with backup_mod.Backup(token) as b:
        return await b.list()


@capability("backup_delete", "删除备份", {"backup_id": {"type": "string"}})
async def _bk_delete(args, *, token, config_dir, base_url):
    async with backup_mod.Backup(token) as b:
        await b.delete(args["backup_id"])
        return {"deleted": args["backup_id"]}


# ----------------- 蓝图 -----------------
@capability("blueprint_list", "列出某域的蓝图", {"domain": {"type": "string"}})
async def _bp_list(args, *, token, config_dir, base_url):
    async with blueprint_mod.Blueprints(token) as bp:
        return await bp.list(args.get("domain", "automation"))


@capability("blueprint_inputs", "读取蓝图声明的输入定义",
            {"domain": {"type": "string"}, "path": {"type": "string"}})
async def _bp_inputs(args, *, token, config_dir, base_url):
    async with blueprint_mod.Blueprints(token) as bp:
        return await bp.inputs(args.get("domain", "automation"), args["path"])


# ----------------- 集成部署 -----------------
@capability("deploy_integration", "从 GitHub 仓库部署 custom_component 到孪生",
            {"owner_repo": {"type": "string"}, "domain": {"type": "string"}})
async def _deploy(args, *, token, config_dir, base_url):
    res = deploy_integration.deploy(args["owner_repo"], config_dir, args.get("domain"))
    return res.as_dict()


if __name__ == "__main__":
    import json
    print(json.dumps(list_capabilities(), ensure_ascii=False, indent=2))
