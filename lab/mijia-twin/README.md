# 米家「中枢网关」· 数字孪生仿真实验室 (mijia-twin)

把用户**真实部署**的 Home Assistant（米家「中枢网关」HA Core，源自台式机141）整套
吸纳进来，在 Devin 的虚拟机里 **1:1 复刻成一个数字孪生沙盒**——用来大规模推演、构建、
压测，而**与用户真机零接触**（不动一个真实设备）。

> 道法自然：本层不含任何模型、不调任何外部推理端点。智能（agent 本体）是在其上操作的
> Devin/外部操作者；这里只提供**确定性的、可复现的孪生世界**。

## 它由什么构成

| 部分 | 来源 | 说明 |
|---|---|---|
| `config/automations.yaml` `scripts.yaml` `scenes.yaml` | 用户真机**原样** | 18+ 自动化、55 脚本、8 场景 |
| `config/packages/` `template_sensors.yaml` `sensors/` | 用户真机**原样** | 模板传感器、意图、移动端联动等 |
| `config/ui-lovelace.yaml` `themes.yaml` | 用户真机**原样** | 主仪表盘与主题 |
| `config/custom_components/lab_sim/` | **Devin 新建** | 数字孪生设备层：复刻真机引用但本地不存在的 **205 个外部设备实体** |
| `config/configuration.yaml` | Devin 改写 | 剔除真机专属耦合（网关 MQTT/TLS、recorder eMMC 路径、受信代理），接入 `lab_sim` |
| `config/packages/devin_lab.yaml` | **Devin 推演新增** | 派生传感器 + 定时任务 + 情景自动化 |
| `config/devin_dashboard.yaml` | **Devin 推演新增** | 「Devin 孪生台」前端仪表盘 |

> 已剔除一切敏感物：网关 X.509 证书/私钥、运行期 `.storage`（鉴权令牌）、数据库与日志，
> 均**不入库**。

## lab_sim —— 数字孪生设备层

真机的设备来自 Xiaomi Home / MQTT / EcoFlow 等集成；这些在沙盒里不存在。`lab_sim`
扫描用户配置引用到、但本地缺失的全部 `entity_id`，**只为「缺口」生成模拟实体**
（已存在的模板/辅助/真实实体一律不碰，零冲突），让真机的自动化/脚本/场景/Lovelace
在孪生里全部跑得起来：

- 灯 23 · 开关 37 · 风扇 2 · 传感器 113（温湿度/功率/电量/储能…按语义给合理初值）
  · 二元传感器 2 · 媒体播放器 7 · number 2 · select 16 · 锁 1 · 摄像头 2 = **205**
- 全部**可交互**：`light.turn_on` / `media_player.play_media` / `lock.unlock` … 状态实时翻转，
  自动化与脚本因此能产生可见联动。

实体清单见 `config/custom_components/lab_sim/entities.json`（由配置自动抽取，去除服务名误判、
配置自身已定义的实体、glob 残片）。

## 在本机跑起来

```bash
docker run -d --name ha-lab -p 8123:8123 \
  -v "$PWD/lab/mijia-twin/config:/config" \
  ghcr.io/home-assistant/home-assistant:stable
# 首访 http://localhost:8123 完成 onboarding
```

启动后实测：354 个实体在线；运行真机脚本 `script.home_mode`（回家模式）能点亮孪生灯具、
打开 Sonoff 插座；`script.play_music_all_speakers` 驱动小爱音响矩阵。

## Devin 推演新增（`devin_lab.yaml` + `devin_dashboard.yaml`）

在孪生之上直接构建并**实测通过**，演示「对话式」为用户设计能力：

- **派生传感器**：`Devin 家庭总功率`（聚合全部功率传感器）、`Devin 灯具在线数`。
- **定时任务**：每天 23:00 汇总总功率与亮灯数，写入 `Devin 最近播报`。
- **情景自动化**：`Devin 夜间守护`——占用传感器持续无人 5 分钟且守护开启时自动关主灯。
- **前端 UI**：侧栏新增「Devin 孪生台」仪表盘（总览 + 音响两视图）。

> 这些只是孪生上的**起手式**。后续可继续在此沙盒推演：新集成、新设备类型、自动化蓝图、
> 更深的前端工作区——全部先在孪生里验证，再考虑是否落到真机。
