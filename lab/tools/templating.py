#!/usr/bin/env python3
"""模板 / Jinja 域工具 (Templating Toolkit) — 先校验后部署的模板传感器构造器。

HA 的模板传感器威力大但易写错；用户常常存了配置、重载后才发现渲染报错。
本工具用 HA 的 `/api/template` 渲染端点**先验证 Jinja**(对着实时状态求值)，
验证通过再写入 `template:` 包并热重载，杜绝"部署即报错"。

    t = Templating(config_dir, token)
    print(t.render("{{ states('sensor.x') }}"))          # 先验证
    t.create_sensor(name="在线灯数", state="{{ states.light|selectattr('state','eq','on')|list|count }}")
    t.reload()
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import yaml

TEMPLATE_PACKAGE = "packages/devin_templates.yaml"


class TemplateError(RuntimeError):
    pass


class Templating:
    def __init__(self, config_dir: str, token: str,
                 base_url: str = "http://127.0.0.1:8123") -> None:
        self.config_dir = config_dir
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _api(self, method: str, path: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw and path != "/api/template" else raw.decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise TemplateError(f"{path} HTTP {exc.code}: {detail}") from exc

    def render(self, template: str) -> str:
        """对着实时状态渲染模板，返回结果或抛出带详情的 TemplateError。"""
        return self._api("POST", "/api/template", {"template": template})

    def validate(self, template: str) -> tuple[bool, str]:
        try:
            return True, self.render(template)
        except TemplateError as exc:
            return False, str(exc)

    def create_sensor(self, name: str, state: str, *, unit: str | None = None,
                      device_class: str | None = None, state_class: str | None = None,
                      icon: str | None = None, attributes: dict | None = None,
                      kind: str = "sensor", validate: bool = True) -> dict:
        """创建/替换一个模板传感器(默认先校验 state 模板)。kind: sensor|binary_sensor。"""
        if validate:
            ok, msg = self.validate(state)
            if not ok:
                raise TemplateError(f"state 模板校验失败: {msg}")
        entry: dict = {"name": name, "state": state}
        if unit:
            entry["unit_of_measurement"] = unit
        if device_class:
            entry["device_class"] = device_class
        if state_class:
            entry["state_class"] = state_class
        if icon:
            entry["icon"] = icon
        if attributes:
            entry["attributes"] = attributes

        path = os.path.join(self.config_dir, TEMPLATE_PACKAGE)
        doc = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle) or {}
        blocks = doc.get("template") or []
        # 找到/新建对应 kind 的 block
        target = next((b for b in blocks if kind in b), None)
        if target is None:
            target = {kind: []}
            blocks.append(target)
        items = [e for e in target[kind] if e.get("name") != name]
        items.append(entry)
        target[kind] = items
        doc["template"] = blocks
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)
        return entry

    def reload(self) -> None:
        self._api("POST", "/api/services/template/reload", {})
