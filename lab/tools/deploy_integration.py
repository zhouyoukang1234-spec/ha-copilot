#!/usr/bin/env python3
"""集成部署引擎 (Integration Deployment Engine).

确定性地把任意 GitHub 上的 Home Assistant **custom_component** 部署进一个 HA 配置目录：

    拉取(GitHub zip) → 定位 custom_components/<domain> → 装入 → 解析 manifest →
    报告 requirements/依赖/版本 → 交由调用方做配置校验与加载核验。

设计原则（道法自然）：
- 无外部 SDK 依赖，仅标准库；可被 Devin 直接调用，也可被 ha_copilot 能力层包装为工具。
- 幂等：重复部署同一 domain 会覆盖旧版本（保留备份）。
- 只做"装入"，不私自重启用户系统；校验/重启由编排层显式触发，职责分明。

用法：
    python deploy_integration.py <owner/repo[@ref]> --config /path/to/config [--domain X]
    python deploy_integration.py --list --config /path/to/config
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field


@dataclass
class DeployResult:
    domain: str
    version: str | None
    source: str
    requirements: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    files: int = 0
    dest: str = ""
    backed_up: bool = False

    def as_dict(self) -> dict:
        return self.__dict__


def _default_branch(owner_repo: str) -> str | None:
    """Ask the GitHub API for the repo's default branch."""
    try:
        url = f"https://api.github.com/repos/{owner_repo}"
        req = urllib.request.Request(url, headers={"User-Agent": "ha-lab-deployer"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("default_branch")
    except Exception:  # noqa: BLE001
        return None


def _download_repo_zip(owner_repo: str, ref: str | None) -> bytes:
    """Download a GitHub repo archive as bytes, trying common refs."""
    owner, repo = owner_repo.split("/", 1)
    if ref:
        refs = [ref]
    else:
        refs = ["main", "master"]
        default = _default_branch(owner_repo)
        if default and default not in refs:
            refs.insert(0, default)
    last_err: Exception | None = None
    for r in refs:
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{r}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ha-lab-deployer"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    # fall back to a tag ref
    if ref:
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/tags/{ref}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ha-lab-deployer"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"无法下载 {owner_repo} (refs={refs}): {last_err}")


def _find_components(zf: zipfile.ZipFile) -> dict[str, list[str]]:
    """Map domain -> list of member paths under custom_components/<domain>/."""
    comps: dict[str, list[str]] = {}
    for name in zf.namelist():
        parts = name.split("/")
        # archives are <repo>-<ref>/custom_components/<domain>/...
        if "custom_components" in parts:
            idx = parts.index("custom_components")
            if len(parts) > idx + 2 and parts[idx + 1]:
                domain = parts[idx + 1]
                comps.setdefault(domain, []).append(name)
    return comps


def deploy(owner_repo: str, config_dir: str, domain: str | None = None,
           ref: str | None = None) -> DeployResult:
    if "@" in owner_repo:
        owner_repo, ref = owner_repo.split("@", 1)

    data = _download_repo_zip(owner_repo, ref)
    zf = zipfile.ZipFile(io.BytesIO(data))
    comps = _find_components(zf)
    if not comps:
        raise RuntimeError(f"{owner_repo} 中找不到 custom_components/<domain>")
    if domain is None:
        if len(comps) > 1:
            raise RuntimeError(
                f"{owner_repo} 含多个集成 {list(comps)}, 请用 --domain 指定")
        domain = next(iter(comps))
    if domain not in comps:
        raise RuntimeError(f"{owner_repo} 中无集成 '{domain}', 可选: {list(comps)}")

    dest_root = os.path.join(config_dir, "custom_components")
    dest = os.path.join(dest_root, domain)
    os.makedirs(dest_root, exist_ok=True)

    backed_up = False
    if os.path.isdir(dest):
        bak = dest + ".bak"
        shutil.rmtree(bak, ignore_errors=True)
        shutil.move(dest, bak)
        backed_up = True

    # extract just this component, stripping the leading <repo>-<ref>/custom_components/<domain>/
    members = comps[domain]
    prefix = members[0].split("custom_components/")[0] + f"custom_components/{domain}/"
    count = 0
    for name in members:
        if name.endswith("/"):
            continue
        rel = name[len(prefix):]
        if not rel:
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(name) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        count += 1

    manifest_path = os.path.join(dest, "manifest.json")
    version = None
    requirements: list[str] = []
    dependencies: list[str] = []
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            man = json.load(handle)
        version = man.get("version")
        requirements = man.get("requirements", [])
        dependencies = man.get("dependencies", [])

    return DeployResult(
        domain=domain, version=version, source=f"{owner_repo}@{ref or 'default'}",
        requirements=requirements, dependencies=dependencies, files=count,
        dest=dest, backed_up=backed_up,
    )


def list_installed(config_dir: str) -> list[dict]:
    root = os.path.join(config_dir, "custom_components")
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        mpath = os.path.join(root, name, "manifest.json")
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as handle:
                man = json.load(handle)
            out.append({"domain": name, "name": man.get("name"),
                        "version": man.get("version"),
                        "requirements": man.get("requirements", [])})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="HA 集成部署引擎")
    ap.add_argument("source", nargs="?", help="owner/repo[@ref]")
    ap.add_argument("--config", required=True, help="HA 配置目录")
    ap.add_argument("--domain", help="当仓库含多个集成时指定")
    ap.add_argument("--list", action="store_true", help="列出已装集成")
    args = ap.parse_args()

    if args.list:
        print(json.dumps(list_installed(args.config), ensure_ascii=False, indent=2))
        return 0
    if not args.source:
        ap.error("需要 source 或 --list")
    res = deploy(args.source, args.config, args.domain)
    print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
