# HA-Copilot · Home Assistant 的操作本源

> 道法自然 · 无为而无不为

本仓库的本源是：**让操作者本身（强 AI / 外部 agent）全链路操作 Home Assistant 的底层**。这里的"智能体"是操作者自己，**不是**一个被塞进聊天框、寄生在外接模型上的弱模型。基础设施只是适配于操作者的"工具层"，操作者直接驱动它，在不断实践中操作到底、验证到底。

因此本组件**不内置任何模型，也不调用任何推理端点**（无 Ollama、无 OpenAI Key、无 base_url）。它只把整台 Home Assistant 的操作面收敛成**一套确定性工具层**（142 个工具），并经**五条本源底层**暴露给外部操作者：

- **底层一 · 原生 HA 服务**：`ha_copilot.run_tool` 通用服务 + 12 个原生资源服务（`ha_copilot.discover_resources` / `ha_copilot.search_zwave_devices` 等），自动化/脚本/开发者工具可直调。每次调用自动发射 `ha_copilot_tool_called` 事件。
- **底层二 · MCP**：鉴权的 MCP 服务器端点 `/api/ha_copilot/mcp`（JSON-RPC 2.0），任意 MCP 客户端即可发现并操作整台 HA。
- **底层三 · 原生 LLM API**：注册为 HA 原生 LLM API，任何对话代理（OpenAI/Anthropic/Google/Ollama/本地模型）可选择 **HA-Copilot** 作为控制 API，直接获得全部 142 个确定性工具。
- **底层四 · HTTP**：鉴权 HTTP 端点 `/api/ha_copilot/tools`（列目录）、`/api/ha_copilot/run_tool`（执行工具）。
- **底层五 · WebSocket**：HA 原生 WebSocket 命令 `ha_copilot/tools`（列目录）、`ha_copilot/run_tool`（执行工具）、`ha_copilot/info`（集成状态）——前端面板和 WS 客户端的实时通道。

五条底层共用**同一套** `tools.py` 工具层——一处实现，五路暴露。

```
操作者本体(外部 agent / 对话代理 / 我)
        │
        ├── MCP 客户端 ──▶ /api/ha_copilot/mcp ────────┐
        ├── 原生 LLM API ──▶ HA 对话代理框架 ────────────┤
        ├── HA 服务 ──▶ 13 个原生服务（自动化可直调）──────┤
        ├── WebSocket ──▶ ha_copilot/* 命令 ────────────────┤──▶ tools.py（142 确定性工具）──▶ 运行中的 HA
        └── HTTP ──▶ /api/ha_copilot/run_tool ────────────┘
```

侧边栏还提供一个**纯确定性、无模型**的工作区面板（类 VS Code）：左侧活动栏在总览/设备/自动化/配置编辑/日志/集成之间切换，右侧"命令台"可直接执行任意工具——与 MCP 暴露的是同一工具层。

### 原生 HA 服务（12 个资源服务）

自动化/脚本可直接调用，返回结构化响应数据：

```yaml
# 一句话搜全 9 源
service: ha_copilot.discover_resources
data:
  query: "xiaomi vacuum"
  limit: 5
response_variable: results

# 查 Z-Wave 设备兼容性
service: ha_copilot.search_zwave_devices
data:
  query: "fibaro fgs213"
response_variable: zwave_results
```

完整服务列表：`discover_resources` · `search_hacs` · `search_github` · `search_blueprints` · `search_zigbee_devices` · `search_zwave_devices` · `search_tasmota_devices` · `search_esphome_devices` · `search_ha_integrations` · `search_ha_addons` · `recommend_resources` · `recommend_blueprints`

## 工具层（操作者可调用的底层能力）

| 工具 | 作用 |
|---|---|
| `list_states` / `get_state` | 列出/读取实体状态与属性 |
| `list_services` / `call_service` | 列出并调用**任意** HA 服务 |
| `list_dir` / `read_config_file` / `write_config_file` | 浏览与读写 config 目录内的文件（写入自动备份 `.copilot.bak`，限制在 config 目录内） |
| `check_config` | 校验配置是否有效 |
| `create_automation` / `delete_automation` | 追加自动化到 `automations.yaml`（按 id/alias 删除）并重载 |
| `create_scene` / `delete_scene` / `create_script` / `delete_script` | 追加/删除场景（`scenes.yaml`）与脚本（`scripts.yaml`）并重载 |
| `list_config_entries` / `reload_config_entry` | 列出已配置集成条目（域/标题/加载状态），按 entry_id 重载某集成（不重启 HA） |
| `create_area` | 在区域注册表中新建房间/区域（幂等） |
| `rename_entity` / `assign_entity_area` / `set_entity_enabled` | 实体注册表写操作：改显示名 / 分配区域 / 启用·禁用 |
| `render_template` | 针对实时状态渲染 Jinja2 模板 |
| `get_history` | 查询实体最近 N 小时的状态历史（需 recorder） |
| `reload` / `restart` | 重载某域配置 / 重启 HA（重启默认禁用） |
| `list_areas` / `registry_overview` | 区域、实体/设备/区域注册表概览 |
| `read_logs` | 读取 HA 日志尾部用于排错 |
| `search_community_resources` | 检索 **HACS** 全量目录（自定义集成 / 前端卡片 / 主题）按品牌·设备·关键词 |
| `search_ha_integrations` | 检索 **Home Assistant 内置集成目录**（约 1470 个）：小白输入品牌或需求（"aqara"、"tuya"、"vacuum"）即知哪些是 HA **原生支持**（无需装 HACS），含 IoT 类别（本地/云）、类型、质量等级与文档页；与 HACS 检索互补（"设备是否被支持 + 怎么加进来"） |
| `search_ha_addons` | 检索 **HA 加载项商店**（官方 + 知名社区库：Mosquitto/Zigbee2MQTT/ESPHome/Matter Server/deCONZ 等）。匹配硬件后常需配套加载项，输入需求（"mqtt"、"zigbee2mqtt"、"matter"、"backup"）即得可安装加载项及其商店/slug/页面 |
| `search_github` / `search_blueprints` | 在 GitHub 搜 HA 相关仓库/模板/示例、社区蓝图（蓝图检索带**逐级放宽召回阶梯**：自然多词短语也不会返回 0 结果） |
| `discover_resources` | **一句自由文本，一次并发搜全部来源**（HACS 目录 + GitHub 仓库 + 社区蓝图 + Zigbee 设备库），返回各来源结果与一份**跨源去重融合的 `top` 榜**（按跨源命中数→stars 排序）。小白输入品牌或需求即可同时拿到“硬件是否被 zigbee2mqtt/zha 支持”+ 可装集成/卡片 + 示例仓库 + 可导入蓝图 |
| `search_zigbee_devices` | 在社区 **Zigbee 设备库**（blakadder，约 2700 款）里按品牌/型号查设备，返回它被哪些桥接支持——尤其是否 **zigbee2mqtt** / zha——及设备参考页。接入蓝图流水线前先确认硬件受支持及所属技术栈 |
| `search_zwave_devices` | 在社区 **Z-Wave 设备库**（zwave-js/node-zwave-js，约 2375 款）里按品牌/型号查设备，返回制造商、型号标签及设备配置文件链接（含参数/关联组）；从 git tree + manufacturers.json 零成本建品牌+型号索引，无需逐文件抓取 |
| `search_tasmota_devices` | 在社区 **Tasmota 模板库**（blakadder，约 2800 款）里按品牌/型号查 ESP8266/ESP32 设备，返回**可直接刷写的 Tasmota 模板**（GPIO 配置）+ 参考页；上游 JSON 含少量畸形条目，逐对象容错解析、跳过坏项不影响整库 |
| `search_esphome_devices` | 在社区 **ESPHome 设备库**（devices.esphome.io，约 770 款）里按品牌/型号查设备，返回 ESP 主板型号、设备类型、是否官方 *made for ESPHome* 及配置页；从仓库 git tree 零成本建 slug 索引，仅对命中项抓取 frontmatter |
| `list_repo_blueprints` | 把一个 GitHub 仓库（`owner/name` 或 URL）解析成其中所有蓝图的 **可直接导入的 raw .yaml URL**，闭合 search→import 链路（两级探测：标准 `blueprints/` 目录 + 根目录单文件内容嗅探） |
| `recommend_resources` | **读取运行中 HA 的真实设备（厂商/集成/实体域），一次调用融合推荐 HACS 集成 + 前端卡片 + 现成自动化蓝图**（各带匹配理由；小白零参数即可被推荐）；并按你拥有的品牌交叉核对 **Zigbee 设备库**，告知这些品牌哪些型号可经 zigbee2mqtt 配对（`zigbee_support`） |
| `recommend_blueprints` | 把真实实体域映射成意图、给出**针对你设备的现成蓝图**；记忆感知（偏好意图前置、已导入仓库降权） |
| `import_blueprint` | 按 URL 把蓝图 YAML 导入运行中的 HA（自动按蓝图内容识别 automation/script/template 域、备份并重载，受 `allow_write` 约束；返回 `loadable` 校验本 HA 版本能否真正加载该蓝图） |
| `validate_blueprint_inputs` / `create_automation_from_blueprint` | 校验/实例化蓝图为真实 automation 或 script（**域自动识别**，script 域蓝图正确落入 `scripts.yaml`；接受 `blueprint_path` 别名，与导入输出无缝衔接） |
| `remember_memory` / `recall_memory` / `list_memory` / `forget_memory` | **跨会话持久记忆**：记住用户偏好/设备备注/历史决策（按 `category` 分类、带时间戳，落盘 `.storage`） |
| `snapshot_device_profile` | 把当前真实设备信号（厂商/集成域/实体域计数）快照进记忆 `devices` 分类，供后续会话直接调用、并据此发现变化 |

### Resource Hub · 把全网资源收敛为可调用的底层

`resources.py` 把"散落在全网的 Home Assistant / 智能家居资源"收敛成确定性工具，两条互补路径取之于网：

- **查询驱动**：`discover_resources(query)` 一句自由文本并发搜 9 个免费数据源（HACS 目录 + GitHub 仓库 + 社区蓝图 + Zigbee 设备库 + Z-Wave 设备库 + Tasmota 模板库 + ESPHome 设备库 + HA 内置集成 + HA 加载项），跨源去重融合成一份 `top` 榜。
- **设备驱动**：`recommend_resources()` 零参数读取用户 HA 里真实的设备厂商与实体域，反向融合推荐该装的集成、前端卡片与现成自动化蓝图——用户什么都不懂，也能被精准推荐。

命中后用 `list_repo_blueprints` → `import_blueprint` → `validate_blueprint_inputs` → `create_automation_from_blueprint` 一条链把蓝图变成运行中的自动化/脚本（域自动识别、导入即校验可加载性、参数别名无缝衔接）。任一来源失败（限流/网络）都独立降级、不影响其它来源（见 `partial_errors`）。全程只读取公开数据、无模型、无外部推理端点；唯一的写操作 `import_blueprint` 受 `allow_write` 约束且限制在 config 目录内。可选导出 `GITHUB_TOKEN` / `GH_TOKEN` 为 HA 进程鉴权以提高 GitHub 检索速率上限。

```bash
# 例：设备驱动——零参数读真实设备，融合推荐集成/卡片/蓝图
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"tool":"recommend_resources","args":{}}' \
  http://<HA>/api/ha_copilot/run_tool

# 例：查询驱动——一句话搜全部来源
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"tool":"discover_resources","args":{"query":"xiaomi vacuum"}}' \
  http://<HA>/api/ha_copilot/run_tool
```

离线/CI 友好的验证脚本：`python hactl/verify_resources.py --live`（实测拉取 HACS 全量目录并验证排序）。

## 原生 HA 实体

通过 UI 配置流（Settings → Integrations → Add Integration → HA-Copilot）安装后，自动创建：

| 实体 | 类型 | 说明 |
|------|------|------|
| `switch.ha_copilot_allow_write` | 开关 | 写配置文件能力开关——可从仪表盘/自动化/语音切换 |
| `switch.ha_copilot_allow_restart` | 开关 | 重启 HA 能力开关 |
| `sensor.ha_copilot_tool_count` | 传感器 | 当前工具目录大小（142） |
| `sensor.ha_copilot_data_sources` | 传感器 | 免费数据源数量（9） |
| `sensor.ha_copilot_native_services` | 传感器 | 原生 HA 服务数量（13） |

安全开关即改即生效，无需重启 HA。传感器为诊断类别，可加入图表。

## 安装

### 方式一 · UI 配置流（推荐）

Settings → Integrations → Add Integration → 搜索 **HA-Copilot** → 设置安全开关 → 完成。无需编辑 YAML。支持中文界面。

### 方式二 · HACS（一键安装）

本仓库自带 `hacs.json`，可作为 **HACS 自定义仓库**安装：

1. HACS → 右上角菜单 → **Custom repositories**；
2. 仓库填 `https://github.com/zhouyoukang1234-spec/ha-copilot`，类别选 **Integration**，添加；
3. 在 HACS 中搜索 **HA-Copilot** 并下载，重启 HA。

### 方式二 · 手动

把 `custom_components/ha_copilot/` 复制到你的 HA config 目录下的 `custom_components/`。

配置是**可选**的，且只含安全开关（没有模型相关项）：

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

- MCP（需 HA 长效令牌），两种传输，同一工具层：
  - **HTTP（JSON-RPC）**：把 `/api/ha_copilot/mcp` 作为端点直接 POST。

```bash
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://<HA>/api/ha_copilot/mcp
```

  - **标准 SSE 传输（2024-11-05）**：开箱即连 Claude Desktop / Cline 等现成 MCP 客户端。客户端连接 `GET /api/ha_copilot/mcp/sse` 收到 `endpoint` 事件后，把 JSON-RPC POST 到该 endpoint，回复经同一 SSE 流返回。

```jsonc
// Claude Desktop / 通用 MCP 客户端配置（SSE）
{
  "mcpServers": {
    "ha-copilot": {
      "url": "http://<HA>/api/ha_copilot/mcp/sse",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

> `get_history` 需要在 `configuration.yaml` 中启用 `recorder:`（或 `default_config:`）。

## 安全

- 所有 HTTP / MCP 端点均需 HA 鉴权（长效令牌）；面板需要管理员权限。
- 文件读写被限制在 HA config 目录内，写入前自动备份。
- `allow_restart` 默认关闭；`allow_write` 可关闭以只读模式运行。

## 状态

v0.3 — **深度融合**：五路暴露（HA 服务 + MCP + 原生 LLM API + HTTP + WebSocket）；UI 配置流（无需 YAML）；安全开关实体（switch）+ 诊断传感器（sensor）+ Diagnostics；事件总线集成（`ha_copilot_tool_called`）；中文翻译；12 个原生资源服务可被自动化直调；9 个免费数据源（HACS 2628 · GitHub · 蓝图 · Zigbee 2700+ · Z-Wave 2375+ · Tasmota 2800+ · ESPHome 770+ · 内置集成 1470 · 加载项 78+）；品牌别名映射（Fibaro→Nice Polska 等）；查询分隔符归一化；142 工具 · 64 个 PR 持续迭代验证。
