# HA-Copilot · Home Assistant 的操作本源

> 道法自然 · 无为而无不为

本仓库的本源是：**让操作者本身（强 AI / 外部 agent）全链路操作 Home Assistant 的底层**。这里的"智能体"是操作者自己，**不是**一个被塞进聊天框、寄生在外接模型上的弱模型。基础设施只是适配于操作者的"工具层"，操作者直接驱动它，在不断实践中操作到底、验证到底。

因此本组件**不内置任何模型，也不调用任何推理端点**（无 Ollama、无 OpenAI Key、无 base_url）。它只把整台 Home Assistant 的操作面收敛成**一套确定性工具层**（1049 个工具），并经**五条本源底层**暴露给外部操作者：

- **底层一 · 原生 HA 服务**：`ha_copilot.run_tool` 通用服务 + 12 个原生资源服务（`ha_copilot.discover_resources` / `ha_copilot.search_zwave_devices` 等），自动化/脚本/开发者工具可直调。每次调用自动发射 `ha_copilot_tool_called` 事件。
- **底层二 · MCP**：鉴权的 MCP 服务器端点 `/api/ha_copilot/mcp`（JSON-RPC 2.0），任意 MCP 客户端即可发现并操作整台 HA。
- **底层三 · 原生 LLM API**：注册为 HA 原生 LLM API，任何对话代理（OpenAI/Anthropic/Google/Ollama/本地模型）可选择 **HA-Copilot** 作为控制 API，直接获得全部 1049 个确定性工具。
- **底层四 · HTTP**：鉴权 HTTP 端点 `/api/ha_copilot/tools`（列目录）、`/api/ha_copilot/run_tool`（执行工具）。
- **底层五 · WebSocket**：HA 原生 WebSocket 命令 `ha_copilot/tools`（列目录）、`ha_copilot/run_tool`（执行工具）、`ha_copilot/info`（集成状态）——前端面板和 WS 客户端的实时通道。

五条底层共用**同一套** `tools.py` 工具层——一处实现，五路暴露。

```
操作者本体(外部 agent / 对话代理 / 我)
        │
        ├── MCP 客户端 ──▶ /api/ha_copilot/mcp ────────┐
        ├── 原生 LLM API ──▶ HA 对话代理框架 ────────────┤
        ├── HA 服务 ──▶ 13 个原生服务（自动化可直调）──────┤
        ├── WebSocket ──▶ ha_copilot/* 命令 ────────────────┤──▶ tools.py（1049 确定性工具）──▶ 运行中的 HA
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
| `diagnose_home` | **全屋诊断**：一次调用返回不可用实体、失败集成、孤儿设备、禁用自动化——所有需要关注的问题 |
| `get_home_context` | **全屋空间树**：楼层→区域→设备→实体（含当前状态），一次调用获得完整家庭画像 |
| `audit_automations` | **自动化审计**：从未触发、闲置 30+ 天、禁用的自动化——主动发现自动化健康问题 |
| `suggest_optimizations` | **优化建议**：未分配实体、无自动化区域、缺失能源监控、无备份自动化——主动改进建议 |
| `check_device_health` | **设备健康**：低电量(<20%)、弱信号(RSSI/link quality)、停滞传感器(48h+)——需要物理维护的设备 |
| `batch_call_service` | **批量服务调用**：一次操作多个实体（如关闭所有灯、设置所有窗帘 50%） |
| `export_config` / `import_config` | **配置导出/导入**：自动化/脚本/场景/仪表盘 YAML 导出备份，导入迁移 |
| `validate_template` | **模板验证**：在嵌入自动化前检查 Jinja2 模板语法 |
| `send_notification` | **发送通知**：通过 HA notify 服务发送通知（默认/指定通知器） |
| `compare_states` | **状态对比**：多实体当前状态并排对比，调试时快速定位差异 |
| `read_logs` | 读取 HA 日志尾部用于排错 |
| `search_community_resources` | 检索 **HACS** 全量目录（自定义集成 / 前端卡片 / 主题）按品牌·设备·关键词 |
| `search_ha_integrations` | 检索 **Home Assistant 内置集成目录**（约 1470 个）：小白输入品牌或需求（"aqara"、"tuya"、"vacuum"）即知哪些是 HA **原生支持**（无需装 HACS），含 IoT 类别（本地/云）、类型、质量等级与文档页；与 HACS 检索互补（"设备是否被支持 + 怎么加进来"） |
| `search_ha_addons` | 检索 **HA 加载项商店**（官方 + 知名社区库：Mosquitto/Zigbee2MQTT/ESPHome/Matter Server/deCONZ 等）。匹配硬件后常需配套加载项，输入需求（"mqtt"、"zigbee2mqtt"、"matter"、"backup"）即得可安装加载项及其商店/slug/页面 |
| `manage_addon` | **管理 Supervisor 加载项**（info/install/start/stop/restart/uninstall），用 search_ha_addons 找到 slug 后直接操作——闭合 搜索→安装→启动 全链路 |
| `setup_integration` | **启动原生集成配置流**：传入域名即开始配置，无需用户输入的集成一步完成，需要输入的返回所需字段描述，再传 user_input 完成设置——闭合 搜索→配置 全链路 |
| `manage_hacs` | **管理 HACS 仓库**（list/install/remove）：搜索到 HACS 集成/前端卡片后直接安装——闭合 搜索→安装 全链路 |
| `get_dashboard_config` | **读取 Lovelace 仪表盘配置**：视图、卡片类型、关联实体——修改仪表盘前先读取当前状态 |
| `update_dashboard` | **保存 Lovelace 仪表盘配置**（存储模式）：从 get_dashboard_config 获取→修改视图/卡片→保存回去——闭合仪表盘完整读写链路 |
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
| `toggle_automation` / `trigger_automation` / `duplicate_automation` | 自动化高级操作：启用/禁用、手动触发（支持 skip_condition）、克隆为新自动化 |
| `remove_device` / `list_device_entities` | 设备注册表高级操作：移除孤儿设备、查询设备下所有实体 |
| `compare_history` | 多实体历史状态并排对比（时序分析） |
| `send_tts` / `play_media` | 媒体播放器操作：TTS 语音播报、播放指定媒体 |
| `activate_scene` / `snapshot_scene` | 场景高级操作：激活（含过渡时间）、快照当前状态为新场景 |
| `publish_mqtt` / `subscribe_mqtt` / `list_mqtt_devices` | **MQTT 协议深度集成**：发布/订阅/设备列表 |
| `permit_zigbee_join` / `rename_zigbee_device` | **Zigbee 协议操作**：开启配对模式（ZHA/Z2M 自适应）、重命名设备 |
| `heal_zwave_network` / `get_zwave_node_info` | **Z-Wave 协议操作**：网络修复、节点详细信息 |
| `wake_on_lan` / `ping_device` | **网络操作**：WoL 唤醒、ICMP 连通性检查 |
| `list_notification_services` / `dismiss_notification` / `create_persistent_notification` | **通知系统**：列出通知目标、创建/清除持久通知 |
| `list_entity_domains` | 列出所有活跃实体域及计数 |
| `start_addon` / `stop_addon` / `restart_addon` / `get_addon_logs` | **加载项生命周期管理**：启动/停止/重启/日志读取 |
| `list_area_devices` / `list_area_entities` | **区域深度查询**：某区域下所有设备、所有实体（含设备间接关联） |
| `delete_blueprint` | 删除蓝图 YAML 文件 |
| `delete_config_entry` / `disable_config_entry` / `reload_integration` | **集成管理**：删除/禁用启用/按域重载 |
| `get_hardware_info` / `get_os_info` | **系统信息**：CPU/内存/磁盘/运行时间、HA OS/Supervisor 版本 |
| `list_template_entities` | 列出所有模板集成创建的实体 |
| `list_credentials` | 列出认证提供者 |
| `media_player_control` | **媒体播放器控制**（14 指令：play/pause/stop/next/previous/volume/source/shuffle/repeat/turn_on/turn_off） |
| `list_media_players` | 列出所有媒体播放器（状态、音源、音量、媒体信息） |
| `send_mobile_notification` | 发送富文本移动推送（支持 actions/images/channels） |
| `get_person_location` / `list_device_trackers` / `get_nearest_person` | **位置追踪**：人物 GPS 坐标、设备追踪器、距区域最近的人 |
| `reload_yaml` / `reload_all_integrations` | **配置重载**：YAML 配置（10 目标）、逐条重载集成 |
| `get_entity_history_summary` / `get_entity_logbook` | **历史/日志**：状态变化摘要、实体日志（recorder 降级容错） |
| `get_states_by_domain` | 按域列出所有实体（含完整属性） |
| `assign_device_label` / `assign_entity_category` | **注册表**：设备标签、实体分类（config/diagnostic） |
| `assign_area_floor` | **楼层管理**：将区域分配到楼层 |
| `scan_tag` | **NFC 标签**：触发 tag_scanned 事件 |
| `add_todo_item` / `remove_todo_item` | **待办事项** CRUD |
| `list_assist_pipelines` / `run_assist_pipeline` | **Assist 语音管道**：列出配置、运行文本 |
| `list_thread_networks` / `get_matter_nodes` | **Thread/Matter**：网络信息、设备节点 |
| `restore_backup` / `download_backup` | **备份管理**：恢复/下载 |
| `increment_counter` / `decrement_counter` / `reset_counter` | **计数器辅助** |
| `start_timer` / `cancel_timer` / `pause_timer` / `finish_timer` | **计时器辅助** |
| `mower_command` | **草坪割草机**：start/pause/dock |
| `valve_control` | **阀门控制**：open/close/set_position/stop |
| `list_event_entities` | 事件实体（最后事件类型） |
| `set_date_value` / `set_time_value` / `set_text_value` | **日期/时间/文本**实体写入 |
| `list_wake_words` / `list_stt_engines` / `list_tts_engines` | **语音系统**：唤醒词、STT/TTS 引擎 |
| `list_conversation_agents` | 对话代理列表 |
| `get_schedule` | 日程安排状态及下次事件 |
| `get_statistics_metadata` / `clear_statistics` | **长期统计**：元数据查询/清除 |
| `send_remote_command` | **红外/射频遥控**：IR/RF 命令发送 |

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
| `sensor.ha_copilot_tool_count` | 传感器 | 当前工具目录大小（414） |
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

### 工具组合 · `run_tools` 步骤间数据流

操作的本源是**搭配**而非堆叠：读取 → 取值 → 执行 → 回读，应在一次请求内闭合，无需每步一轮推理。`run_tools` 顺序执行一批 `{tool, args}`，并让**后一步直接引用前一步的结果**——这是工具组合的核心原语：

- `${steps[i].path}`：第 `i` 步结果中的值（如 `${steps[0].entities[0].entity_id}`）。
- `${last.path}`：上一步结果中的值。
- `${vars.NAME.path}`：某步通过 `save_as` 绑定的结果中的值；名字不与 `steps/last/vars/item/index` 撞名时可简写为 `${NAME.path}`（少写一层前缀，降认知负荷）。

整串 `${...}` 保留被引用对象的原类型（列表/字典）；文本内联 `${...}` 转为字符串。引用失败只让该步优雅报错、不拖垮整批（除非 `stop_on_error`）。

某一步还可带 `foreach`（一个列表，或指向列表的 `${...}` 引用）：该步的 `tool` 会**对每个元素各执行一次**，元素以 `${item}`、其序号以 `${index}` 注入——把"先发现集合、再逐个操作"收敛成单步扇出（如对上一步列出的每个实体执行操作）。每个元素的错误相互隔离，步骤结果为 `{"foreach": true, "count", "errors", "results": [...]}`。

```bash
# 列出所有灯 → 单步 foreach 逐个关灯（引用 ${item.entity_id}）
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"tool":"run_tools","args":{"calls":[
        {"tool":"list_states","args":{"domain":"light"},"save_as":"L"},
        {"tool":"call_service","foreach":"${vars.L.entities}",
          "args":{"domain":"light","service":"turn_off","data":{"entity_id":"${item.entity_id}"}}}
      ]}}' \
  http://<HA>/api/ha_copilot/run_tool
```

```bash
# 读客厅灯状态 → 引用其 entity_id 开灯 → 回读状态，一次请求内完成
curl -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"tool":"run_tools","args":{"calls":[
        {"tool":"get_state","args":{"entity_id":"light.ke_ting_deng"},"save_as":"L"},
        {"tool":"call_service","args":{"domain":"light","service":"turn_on",
          "data":{"entity_id":"${vars.L.entity_id}"}}},
        {"tool":"get_state","args":{"entity_id":"${steps[0].entity_id}"}}
      ]}}' \
  http://<HA>/api/ha_copilot/run_tool
```

### 组织复杂度 · 注册表是元数据的唯一真源

区域 / 楼层 / 标签 / 设备归属**只存在于注册表里**，状态属性里不会有（HA 从不把
`area_id`/`labels`/`floor_id` 写进 `state.attributes`）。要按房间或楼层组织上千实体，
必须查注册表，而非读状态属性——否则会得到"全部未分配"的假象。归属解析遵循回退链：

```
实体.area_id  →（无则）实体.device_id → 设备.area_id  →  区域.floor_id
```

按此真源组织数字孪生的工作流（楼层 → 区域 → 实体，单请求内扇出）：

```bash
# 建楼层 → 建区域并挂到该楼层 → 把若干实体归入该区域（foreach 扇出）
curl ... -d '{"tool":"run_tools","args":{"calls":[
  {"tool":"floor_create","args":{"name":"二楼","level":2},"save_as":"F"},
  {"tool":"area_create","args":{"name":"主卧","floor_id":"${vars.F.floor_id}"},"save_as":"A"},
  {"tool":"assign_entity_area","foreach":"${vars.BEDROOM_LIGHTS}",
    "args":{"entity_id":"${item}","area_id":"${vars.A.area_id}"}}
]}}' ...
```

校验用只读工具直接读注册表：`area_registry_deep`、`floor_registry_check`、
`label_registry_check`、`label_summary`、`floor_plan_entity_status`、`area_entity_count`。

**三条正交组织轴**：楼层/区域答"在哪"（层级、单一归属）；标签答"是什么"
（功能性、横切——不同房间里的一盏灯和一个人体传感器可共享 `lighting`/`security`
标签）。区域用 `assign_entity_area`，标签用 `label_assign_entity`（配 `label_create` /
`label_list_entities` / `label_delete`）。归属真源始终在注册表，故组织上千实体只是
"解析每个实体的归属键 → foreach 扇出赋值"。删标签只认 `label_delete`（`manage_label`
的动作是 `list/create/delete`，无 `remove`）。

### 编排 × 组织的合流 · 按归属发现再扇出

四原语（序 / 引用 / 绑定 / 扇出）与三组织轴合流出一个域无关的宏范式——
**"按归属发现集合 → 逐个扇出正确服务 → 回读校验"**，整串在一次 `run_tools` 内闭合。
无论"离开房间"（按区域）还是"全屋关灯"（按标签），骨架同构：

```bash
# 按区域离场宏：查该区域的灯/窗帘/风扇 → 分别扇出关灯/关帘/停扇 → 回读一个
curl ... -d '{"tool":"run_tools","args":{"steps":[
  {"tool":"entity_list_by_area_domain","args":{"area_id":"u1_f1_living","domain":"light"},"save_as":"L"},
  {"tool":"call_service","foreach":"${vars.L.entities}",
    "args":{"domain":"light","service":"turn_off","data":{"entity_id":"${item.entity_id}"}}},
  {"tool":"get_state","args":{"entity_id":"${vars.L.entities[0].entity_id}"}}
]}}' ...

# 按标签宏：查 lighting 标签全部实体 → homeassistant.turn_off 逐个扇出
#   {"tool":"label_list_entities","args":{"label_id":"lighting"},"save_as":"LI"}
#   {"tool":"call_service","foreach":"${vars.LI.entities}", ...}
```

此范式在数百区域 × 数千实体上逐元素错误隔离、零崩溃：组织提供"集合真源"，
编排提供"无回路的映射"，二者相乘即覆盖绝大多数真实场景（闻道者日损）。

**尺度不变**：同一套工具层 / 组织轴 / 编排原语 / 自治层，在 150 → 2911 → 3841 →
4767 实体逐级放大中指标恒定——全量扫描 0 crash / 0 http、数百步单请求超长链 0 崩溃、
48 线程并发混合读写 0 竞态、注册表零空区域。放大只增数量、不增缺陷（道恒无为而无不为）。

### 验证纪律 · 反者道之动，三个易被忽略的边界

大规模扫描给工具灌通用参数，写类服务永远只是"被优雅拒绝"、从未真正跑通；顺序扫描
也照不出并发下的竞态；正常输入更照不出解析器在畸形输入下会不会崩。把"看起来没问题"
的地方回头再验，才是 `反者道之动`：

- **happy-path 回验**：对每个可控域取一个**能力匹配**的真实实体，灌**有效**参数跑
  "改 → 回读 → 校验属性确实动了"（灯亮/帘到位/温度到点/锁上/音量变）。14 个域全部
  端到端跑通、零崩溃——把"honest-on-junk"升级为"verified-on-valid"。
- **并发边界**：48 线程 × 1200 请求混合读写、对同一批灯/标签/区域制造争用，之后回查
  注册表完整性（区域数不变、零空区域、标签仍为规范集）。0 crash / 0 http / 0 竞态，
  证明工具层写路径线程安全。
- **对抗性边界**：向 `run_tools` 灌 26 类畸形/退化/敌意输入——`foreach` 喂整数/字典/
  空表、越界索引、未闭合与空 `${}`、深路径缺字段、20 万字符长串、Unicode 与注入片段、
  非字典步骤、2000 步大批、200 元素错误风暴。全部**逐个优雅隔离、0 崩溃**；且保留字
  与命名空间隔离——`save_as:"last"` 只写进 `vars`，不污染内建 `${last}`；`run_tools`
  显式拒绝嵌套（`cannot be nested`）而非崩溃。健壮性来自"错误隔离"这一条原语横切全链。

### 仪表盘组合 · 从注册表生成多视图 Lovelace

仪表盘是组织结构的**投影**：先从注册表读出楼层/区域/实体，再机器生成视图，
而非手工拼卡片。三类工具闭合"建 → 读 → 改 → 删"：

- `create_dashboard`（`url_path` 唯一即可，支持单词名）：新建存储型仪表盘并注册侧边栏面板，可同时用 `config` 注入初始视图。
- `get_dashboard_config` / `update_dashboard`：读回、整体改写某仪表盘的视图配置。
- `list_dashboards` / `delete_dashboard`：枚举、按 `url_path` 删除（默认 `lovelace` 受保护）。

```bash
# 用注册表数据生成"每楼层一视图 + 每域一视图"的复杂仪表盘，一次建好
curl ... -d '{"tool":"create_dashboard","args":{
  "url_path":"dao-twin","title":"DAO Twin","icon":"mdi:sitemap",
  "config":{"title":"DAO Twin","views":[
    {"title":"二楼","path":"floor2","cards":[{"type":"entities","title":"主卧","entities":["light.zhu_wo_deng"]}]},
    {"title":"灯光","path":"lighting","cards":[{"type":"entities","entities":["light.a","light.b"]}]}
  ]}}}' ...
```

配方：①`floor_registry_check`/`area_registry_deep` 取骨架 → ②按 `floor_id` 把区域分组、
每楼层一视图 → ③再按域（light./climate./lock. …）切若干 `entities` 卡做横向总览 →
④`create_dashboard` 一次写入。改版只需重算 `config` 再 `update_dashboard`/重建。

### 自治层 · 沙盒与本尊分离（自愈不误伤真内容）

大规模扫描给写类工具灌探针参数，会把垃圾条目写进 `automations.yaml` / `scenes.yaml`，
所以自愈循环把这些默认文件**整体清空**——但这也会连真自动化一起抹掉。根因解法（道法自然）：
**把探针沙盒与规范本尊分离**。真实自治内容（场景 / 脚本 / 自动化）写进一个自愈从不触碰的
独立包 `packages/dao_canon.yaml`；默认文件只留给探针，随清随建。

自治层是组织结构的**行为投影**，四种形态齐备：**声明式**（每层全关场景）、**过程式**
（离家脚本多域扇出关灯/关帘/上锁）、**手动触发**（拨 helper → 自动化 → 脚本）、以及
**事件驱动/反应式**——一条模板化自动化监听一批 `binary_sensor.*_motion`，用
`trigger.to_state.entity_id` 现推出同房间的 `light.*_ceiling`，有人则亮、无人则灭
（`mode: queued`，不在数千实体上乱触发）。实测两条闭环都通：拨 helper → 灯全灭门全锁；
制造 motion → 对应吊灯亮/灭 3/3。且跑完一次自愈后，`automations.yaml` 里的垃圾被清、
`dao_canon.yaml` 的本尊**原样存活**——沙盒与本尊分离让自治层与边界扫描各行其道。

- MCP（需 HA 长效令牌），两种传输，同一工具层。两条链路均已端到端实测：`initialize`
  回 `protocolVersion 2024-11-05` + capabilities，`tools/list` 出全部 2115 个工具，
  `tools/call` 既能调单工具、也能跑带 `save_as`/`${vars...}` 数据流的 `run_tools` 组合
  （`isError:false`）；SSE 首帧即 `event: endpoint` 带会话作用域回投 URL。
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

v1.0 — **全域覆盖**：414 个确定性工具覆盖 HA 全部已知平台域——从基础状态/服务/自动化/脚本/场景到高级平台：媒体播放器（14 指令）、位置追踪（人/设备/最近距离）、YAML/集成重载、历史摘要/日志、设备注册表、楼层管理、NFC 标签、待办事项、Assist 语音管道、Thread/Matter 网格、备份管理、计数器/计时器辅助、草坪割草机、阀门控制、事件实体、日期/时间/文本实体、唤醒词、STT/TTS 引擎、对话代理、日程安排、长期统计、红外遥控。五路暴露（HA 服务 + MCP + 原生 LLM API + HTTP + WebSocket）；UI 配置流；安全开关实体 + 诊断传感器；事件总线集成；中文翻译；12 个原生资源服务；9 个免费数据源；品牌别名映射；查询归一化；写保护 + 破坏性操作分类 + MCP 注解。100 个 PR 持续迭代验证。
