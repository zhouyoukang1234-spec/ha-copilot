#!/usr/bin/env python3
"""编程化构造器 (Authoring Toolkit) — 自动化 / 场景 / 仪表盘的"对话式部署"底座。

把"帮用户设计自动化/场景/前端"这件事变成确定性、可校验、可热重载的能力：
Devin(或任意经能力层接入的 agent)给出高层意图 → 本工具写入受管 YAML →
经 HA API 热重载(无需整机重启)→ 立即生效。

受管文件(只动 Devin 自己的命名空间，不污染用户真机原样配置)：
    packages/devin_authored.yaml      ← 自动化(本工具维护)
    devin_dashboard.yaml              ← 仪表盘(generate_dashboard)

用法(库)：
    from author import Authoring
    a = Authoring(config_dir, base_url, token)
    a.create_automation(alias=..., trigger=[...], action=[...])
    a.reload_automations()
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request

import yaml

MANAGED_AUTOMATIONS = "packages/devin_authored.yaml"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not s or s == "devin":
        # non-ASCII alias slugifies to nothing — derive a stable suffix from it
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        s = f"{s}_{digest}" if s else f"auto_{digest}"
    return s


class Authoring:
    def __init__(self, config_dir: str, base_url: str = "http://127.0.0.1:8123",
                 token: str | None = None) -> None:
        self.config_dir = config_dir
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ---- HA API ----
    def _api(self, method: str, path: str, body: dict | list | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    # ---- automations ----
    def _managed_path(self) -> str:
        return os.path.join(self.config_dir, MANAGED_AUTOMATIONS)

    def _load_managed(self) -> dict:
        path = self._managed_path()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        return {}

    def create_automation(self, alias: str, trigger: list, action: list,
                          condition: list | None = None, mode: str = "single",
                          auto_id: str | None = None) -> dict:
        """Append/replace an automation by id and persist to the managed package."""
        doc = self._load_managed()
        autos = doc.get("automation") or []
        auto_id = auto_id or f"devin_{_slug(alias)}"
        entry = {"id": auto_id, "alias": alias, "mode": mode,
                 "trigger": trigger, "action": action}
        if condition:
            entry["condition"] = condition
        autos = [a for a in autos if a.get("id") != auto_id]
        autos.append(entry)
        doc["automation"] = autos
        os.makedirs(os.path.dirname(self._managed_path()), exist_ok=True)
        with open(self._managed_path(), "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)
        return entry

    def _save_managed(self, doc: dict) -> None:
        os.makedirs(os.path.dirname(self._managed_path()), exist_ok=True)
        with open(self._managed_path(), "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)

    def reload_automations(self) -> None:
        self._api("POST", "/api/services/automation/reload", {})

    def reload(self, domain: str) -> None:
        """Reload a config-driven domain in place (no restart)."""
        self._api("POST", f"/api/services/{domain}/reload", {})

    # ---- scenes ----
    def create_scene(self, name: str, entities: dict, scene_id: str | None = None) -> dict:
        """Append/replace a scene (snapshot of entity states) in the managed package."""
        doc = self._load_managed()
        scenes = doc.get("scene") or []
        scene_id = scene_id or f"devin_{_slug(name)}"
        entry = {"id": scene_id, "name": name, "entities": entities}
        scenes = [s for s in scenes if s.get("id") != scene_id]
        scenes.append(entry)
        doc["scene"] = scenes
        self._save_managed(doc)
        return entry

    # ---- scripts ----
    def create_script(self, alias: str, sequence: list, script_id: str | None = None,
                      mode: str = "single") -> dict:
        """Append/replace a script (named action sequence) in the managed package."""
        doc = self._load_managed()
        scripts = doc.get("script") or {}
        script_id = script_id or _slug(alias)
        scripts[script_id] = {"alias": alias, "mode": mode, "sequence": sequence}
        doc["script"] = scripts
        self._save_managed(doc)
        return {"script_id": script_id, **scripts[script_id]}

    # ---- helpers (input_*, timer, counter) ----
    def create_helper(self, domain: str, object_id: str, config: dict) -> dict:
        """Define a helper entity (input_boolean/number/text/select/datetime, timer, counter)."""
        doc = self._load_managed()
        helpers = doc.get(domain) or {}
        helpers[object_id] = config
        doc[domain] = helpers
        self._save_managed(doc)
        return {"entity_id": f"{domain}.{object_id}", **config}

    def list_automations(self) -> list[str]:
        states = self._api("GET", "/api/states")
        assert isinstance(states, list)
        return [e["entity_id"] for e in states if e["entity_id"].startswith("automation.")]

    # ---- dashboards ----
    def generate_dashboard(self, filename: str, title: str, views: list) -> str:
        """Write a Lovelace YAML dashboard file (referenced from configuration.yaml)."""
        path = os.path.join(self.config_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump({"title": title, "views": views}, handle,
                           allow_unicode=True, sort_keys=False)
        return path
