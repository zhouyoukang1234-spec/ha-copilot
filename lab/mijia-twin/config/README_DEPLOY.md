# 网关 HA 配置包
生成时间: 2026-03-29T00:03:27.765281
来源: 台式机141 HA Docker + 本地文件

## 统计
- 自动化: 18 条
- 脚本: 0 个
- 场景: 8 个
- 写入文件: 41 个
- 警告: 0 条

## 部署方式

### 自动(推荐)
```bash
python gateway_ha_deploy.py
```

### 手动
```bash
# 1. 通过telnet连到网关
# 2. 创建目录
mkdir -p /data/ha/config

# 3. 上传本目录所有文件到 /data/ha/config/
# 4. 上传certs/到 /data/ha/certs/

# 5. 安装HA Core
sh /tmp/ha_installer.sh

# 6. 启动
/data/ha/start.sh
```

## 剪裁说明
- ❌ 删除: PowerShell shell_command (Windows专用)
- ❌ 删除: HACS /hacsfiles/ JS资源
- ❌ 删除: hassio panel_custom (需要supervisor)
- ✅ 修改: recorder db_url → /data/ha/config/
- ✅ 修改: recorder purge_keep_days: 365→14
- ✅ 修改: commit_interval: 1→60 (减少eMMC写入)
- ✅ 新增: MQTT → 网关自身:8883 (TLS+X.509)
- ✅ 保留: 所有自动化/脚本/场景/传感器模板

## 需要登录后手动配置
1. 首次访问 http://192.168.31.53:8123 创建管理员账户
2. 添加 Xiaomi Home 集成 (需要小米账号登录)
3. 添加 eWeLink 集成 (Sonoff设备)
4. 配置手机IP (MacroDroid当前IP: 192.168.31.40)
