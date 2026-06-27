# 真机配置缺陷修复清单（轮次10）

孪生 = 你真机配置的 1:1 复刻。以下缺陷在孪生里暴露并已修复；**同名问题同样存在于你真机**，建议同步。
全部经 `check_config` 验证：修复后配置校验从 5 类错误降到 0 错误。

| # | 文件 | 缺陷 | 后果 | 修复 |
|---|---|---|---|---|
| 1 | `groups.yaml` | 7 个 group 键名被 delta2 / river2 两台设备**重复使用**（battery_status / power_io / temperature_monitoring / timeout_management / battery_settings / control_switches / miscellaneous） | YAML 后者覆盖前者，**delta2 的 7 个分组被静默丢弃** | 键名加设备后缀 `_delta2` / `_river2`，14 个分组全部保留 |
| 2 | `packages/entertainment_scripts.yaml` | `script:` 块里 3 个脚本（set_all_speakers_volume 等）**与 scripts.yaml 重复定义** | HA 拒绝重复 script id，**整个 entertainment_scripts 包加载失败**（含其自动化） | 删除重复的 `script:` 块（脚本已在 scripts.yaml），保留 `automation:` |
| 3 | `automations.yaml` · 智能开启户外电源插头 | 顶层写了 `enabled: true`，但本版 HA 自动化 schema **不接受 `enabled` 键** | 该自动化校验失败被**禁用**（与意图相反） | 删除 `enabled: true`（默认即启用） |
| 4 | `packages/accessibility.yaml` | 引用不存在的集成 `accessibility`（上一个 AI 幻觉产物 "Cycle #5"） | 包加载失败报错 | 停用为 `.disabled` |
| 5 | `packages/ai-voice-commands.yaml` | 文件名含连字符（非法 slug）+ 引用不存在的 `voice_processor` | 包从不初始化 | 停用为 `.disabled` |
| 6 | `packages/conversation.yaml` | 顶层 `intents:` 不是真实集成 | 包加载失败 | 改用 HA 真实机制 `custom_sentences/zh-cn/devin_intents.yaml` |

## 关于中文语音（#6 的正确实现）

原 `conversation.yaml` 用一个并不存在的 `intents:` 集成，从根上无法工作。已用 HA 官方支持的
`custom_sentences/<lang>/` 机制重写，挂在内置 `conversation` + `default_config` 上，**无需任何外部模型**。

实测：`POST /api/conversation/process {"text":"打开床底灯","language":"zh-cn"}`
→ `床底灯已打开`，`light.chuang_di_deng` off→on。

## 仍未修（需你决策 / 提供）

- `automations.yaml` · 每日下载必应壁纸：调用未定义的 `shell_command.download_bing_wallpaper`（运行时才报错）。
  需你提供该下载命令，或删除此自动化。
- `weather.he_feng_tian_qi_2`（和风天气）：需 QWeather API Key 才能接入。
