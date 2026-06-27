# 孪生审计报告 (watchman 自动暴露 → 修复闭环)

用部署引擎纳入 `watchman` 集成，对孪生执行 `watchman.report`，主动暴露"配置引用了但系统里不存在"的实体/服务。这是"让缺陷不断暴露 → 修复 → 再推"的循环载体。

## 首轮审计基线

```
sensor.watchman_missing_entities = 8
sensor.watchman_missing_actions  = 7
processed_files = 29
```

## 已修复（本轮）

| # | 缺陷 | 性质 | 修复 | 验证 |
|---|---|---|---|---|
| 1 | `text.xiaomi_*_execute_text_directive` ×4 缺失 | **lab_sim 真实缺陷**：从未模拟 `text` 域 | 新增 `lab_sim/text.py` 平台 + 注册到 `PLATFORMS` + 写入 `entities.json` | 4 个 text 实体上线，watchman 列表中消失 |
| 2 | `packages/automations_mobile.yaml` 整包不加载 | **用户真机潜在 bug**：包文件是裸自动化列表，HA 要求 `domain->配置` 字典 | 把列表包进 `automation:` 键 | 9 条移动端自动化加载（早晨唤醒/晚间模式/高温开风扇…），启动错误消除，总自动化 45 |

> 修复 #2 同样存在于用户真机配置中——该包在真机上也从未生效。建议在真机同步修正。

## 待办（需用户决策 / 后续轮次）

| 项 | 说明 |
|---|---|
| `packages/ai-voice-commands.yaml` | 文件名含连字符（HA 拒绝为非法 slug），且内容引用不存在的集成（`voice_processor` 等），疑似 AI 生成的无效配置。`script.sleep_mode` / `script.export_daily_report` 源于此包故缺失。建议：清理或重写该包。 |
| `weather.he_feng_tian_qi_2` | 依赖「和风天气」自定义集成，孪生未安装。后续可用部署引擎纳入。 |
| `notify.mobile_app*` / `tts.*_say` / `shell_command.*` | 依赖 HA 手机伴侣 App / 云 TTS / 主机 shell，孪生环境本无，属预期缺口（可按需打桩）。 |
| `input_text.unified_speaker_command` / `dual_speakers_command` | **非缺陷**：已在 `input_text.yaml` 定义，仅初值为 `unknown`，被 watchman 计入。可配置 watchman 忽略 unknown 态以降噪。 |
