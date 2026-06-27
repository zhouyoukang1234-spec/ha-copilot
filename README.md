# HA-Copilot · Home Assistant 版的 Cursor

> 道法自然 · 无为而无不为

**HA-Copilot** 把 "Cursor 之于 VS Code" 的思路搬进 Home Assistant：它不是一个从外部调用 API 的助手，而是一个**深度融合进 HA 内部**的 AI 协作层。你用自然语言下达意图，AI 直接操作 HA 的底层——读写配置、调用任意服务、查询实体/区域/设备、创建自动化、校验配置、重载与排错——并自我验证结果。

## 它是怎么"融合"的

和 Cursor 一样靠的是"住在里面"。HA-Copilot 是一个 **custom integration**，启动后：

1. 在 HA 侧边栏注册一个**聊天面板**（自定义 web component，无需构建步骤）。
2. 暴露一组**深度操作工具**（见下表），由 LLM 通过 function-calling 直接驱动。
3. 接任意 **OpenAI 兼容**的推理端点；默认指向本地 **Ollama**，无需任何云端 Key。

```
你(自然语言) ──▶ 聊天面板 ──▶ /api/ha_copilot/chat ──▶ Agent 循环(LLM + 工具)
                                                          │
                                              ┌───────────┴───────────┐
                                         读写配置 / 调服务 / 查实体 / 建自动化 / 校验 / 重载 / 读日志
                                              └────────── 直接作用于运行中的 HA ──────────┘
```

## 工具层（AI 可调用的底层能力）

| 工具 | 作用 |
|---|---|
| `list_states` / `get_state` | 列出/读取实体状态与属性 |
| `list_services` / `call_service` | 列出并调用**任意** HA 服务 |
| `read_config_file` / `write_config_file` | 读写 config 目录内的 YAML（写入自动备份 `.copilot.bak`，限制在 config 目录内） |
| `check_config` | 校验配置是否有效 |
| `create_automation` | 追加自动化到 `automations.yaml` 并重载 |
| `create_scene` / `create_script` | 追加场景到 `scenes.yaml` / 脚本到 `scripts.yaml` 并重载（可直接执行） |
| `create_area` | 在区域注册表中新建房间/区域（幂等） |
| `rename_entity` / `assign_entity_area` / `set_entity_enabled` | 实体注册表写操作：改显示名 / 分配区域 / 启用·禁用（等价于 Settings UI 里的操作） |
| `render_template` | 针对实时状态渲染 Jinja2 模板（等价于开发者工具 > 模板） |
| `get_history` | 查询实体最近 N 小时的状态历史（需 recorder） |
| `reload` / `restart` | 重载某域配置 / 重启 HA（重启默认禁用） |
| `list_areas` / `registry_overview` | 区域、实体/设备/区域注册表概览 |
| `read_logs` | 读取 HA 日志尾部用于排错 |

## 安装

把 `custom_components/ha_copilot/` 复制到你的 HA config 目录下的 `custom_components/`，在 `configuration.yaml` 加入：

```yaml
ha_copilot:
  base_url: "http://localhost:11434/v1"   # 任意 OpenAI 兼容端点（默认本地 Ollama）
  model: "qwen2.5:3b"                       # 需支持 function calling 的模型
  # api_key: "sk-..."                       # 仅云端端点需要
  allow_write: true                          # 允许写配置文件
  allow_restart: false                       # 是否允许 AI 重启 HA
```

重启 HA 后，侧边栏出现 **HA-Copilot**。也可在开发者工具里调用服务：

- `ha_copilot.ask`（带响应）—— 用自然语言驱动 AI（LLM + 工具循环）。
- `ha_copilot.run_tool`（带响应）—— 绕过 LLM，直接执行单个底层工具，便于自动化/脚本调用与确定性测试，例如：

```yaml
service: ha_copilot.run_tool
data:
  tool: render_template
  args:
    template: "{{ states.light | selectattr('state','eq','on') | list | count }}"
```

> `get_history` 需要在 `configuration.yaml` 中启用 `recorder:`（或 `default_config:`）。

### 本地模型（推荐 Ollama）

```bash
ollama pull qwen2.5:3b      # 体积小、支持工具调用
ollama serve               # 暴露 http://localhost:11434/v1
```

## 安全

- 文件读写被限制在 HA config 目录内，写入前自动备份。
- `allow_restart` 默认关闭；`allow_write` 可关闭以只读模式运行。
- 面板需要管理员权限。

## 状态

v0.1 — 核心融合层与聊天面板已可用，并已在真实 HA 实例 + 本地 Ollama 上端到端验证。深化的底层工具（注册表读写、场景/脚本、模板渲染、历史查询）已通过确定性闭环测试（写入 → 重载 → 回读/执行 验证），9/9 通过。
