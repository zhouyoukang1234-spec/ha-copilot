# HA-Copilot · Home Assistant 的操作本源

> 道法自然 · 无为而无不为

本仓库的本源是：**让操作者本身（强 AI / 外部 agent）全链路操作 Home Assistant 的底层**。这里的"智能体"是操作者自己，**不是**一个被塞进聊天框、寄生在外接模型上的弱模型。基础设施只是适配于操作者的"工具层"，操作者直接驱动它，在不断实践中操作到底、验证到底。

因此本组件**不内置任何模型，也不调用任何推理端点**（无 Ollama、无 OpenAI Key、无 base_url）。它只把整台 Home Assistant 的操作面收敛成**一套确定性工具层**，并经两条本源底层暴露给外部操作者：

- **底层一 · 直调**：`ha_copilot.run_tool` 服务，以及鉴权 HTTP 端点 `/api/ha_copilot/tools`（列目录）、`/api/ha_copilot/run_tool`（执行单个工具）。
- **底层二 · MCP**：鉴权的 MCP 服务器端点 `/api/ha_copilot/mcp`（JSON-RPC 2.0，支持 `initialize` / `tools/list` / `tools/call`），任意 MCP 客户端（即操作者本体）即可发现并操作整台 HA。

两条底层共用**同一套** `tools.py` 工具层——一处实现，两种暴露。

```
操作者本体(外部 agent / 我)
        │
        ├── MCP 客户端 ──▶ /api/ha_copilot/mcp ──┐
        │                                        ├──▶ tools.py（确定性工具层）──▶ 运行中的 HA
        └── 直调 ──▶ run_tool 服务 / HTTP ────────┘
```

侧边栏还提供一个**纯确定性、无模型**的工作区面板（类 VS Code）：左侧活动栏在总览/设备/自动化/配置编辑/日志/集成之间切换，右侧"命令台"可直接执行任意工具——与 MCP 暴露的是同一工具层。

## 工具层（操作者可调用的底层能力）

| 工具 | 作用 |
|---|---|
| `list_states` / `get_state` | 列出/读取实体状态与属性 |
| `list_services` / `call_service` | 列出并调用**任意** HA 服务 |
| `list_dir` / `read_config_file` / `write_config_file` | 浏览与读写 config 目录内的文件（写入自动备份 `.copilot.bak`，限制在 config 目录内） |
| `check_config` | 校验配置是否有效 |
| `create_automation` | 追加自动化到 `automations.yaml` 并重载 |
| `create_scene` / `create_script` | 追加场景到 `scenes.yaml` / 脚本到 `scripts.yaml` 并重载 |
| `create_area` | 在区域注册表中新建房间/区域（幂等） |
| `rename_entity` / `assign_entity_area` / `set_entity_enabled` | 实体注册表写操作：改显示名 / 分配区域 / 启用·禁用 |
| `render_template` | 针对实时状态渲染 Jinja2 模板 |
| `get_history` | 查询实体最近 N 小时的状态历史（需 recorder） |
| `reload` / `restart` | 重载某域配置 / 重启 HA（重启默认禁用） |
| `list_areas` / `registry_overview` | 区域、实体/设备/区域注册表概览 |
| `read_logs` | 读取 HA 日志尾部用于排错 |

## 安装

把 `custom_components/ha_copilot/` 复制到你的 HA config 目录下的 `custom_components/`。配置是**可选**的，且只含安全开关（没有模型相关项）：

```yaml
ha_copilot:
  allow_write: true       # 允许写配置文件（默认 true）
  allow_restart: false    # 是否允许重启 HA（默认 false）
```

重启 HA 后：

- 侧边栏出现 **HA-Copilot** 工作区面板。
- 直调（开发者工具 > 服务）：

```yaml
service: ha_copilot.run_tool
data:
  tool: render_template
  args:
    template: "{{ states.light | selectattr('state','eq','on') | list | count }}"
```

- 直调（HTTP，需 HA 长效令牌）：

```bash
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"tool":"list_states","args":{"domain":"light"}}' \
  http://<HA>/api/ha_copilot/run_tool
```

- MCP（需 HA 长效令牌）：把 `/api/ha_copilot/mcp` 作为 MCP 服务器端点接入任意 MCP 客户端；`Authorization: Bearer <TOKEN>`。

```bash
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://<HA>/api/ha_copilot/mcp
```

> `get_history` 需要在 `configuration.yaml` 中启用 `recorder:`（或 `default_config:`）。

## 安全

- 所有 HTTP / MCP 端点均需 HA 鉴权（长效令牌）；面板需要管理员权限。
- 文件读写被限制在 HA config 目录内，写入前自动备份。
- `allow_restart` 默认关闭；`allow_write` 可关闭以只读模式运行。

## 状态

v0.2 — **去模型化**：移除内置 LLM agent 与一切外部推理端点耦合，组件收敛为纯能力层。同一工具层经"直调"与"MCP"两条底层暴露，已在真实 HA 实例上端到端验证（`run_tool` 服务 / HTTP、MCP `initialize`·`tools/list`·`tools/call` 均确定性闭环：写入 → 改状态 → 回读校验）。
