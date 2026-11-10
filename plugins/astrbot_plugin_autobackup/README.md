# AstrBot 自动备份插件

AstrBot 自动备份插件，支持定时备份和手动备份，自动排除 `.venv` 目录及其他不必要的文件。

## 功能特性

- **自动备份**: 支持 cron 表达式配置定时备份，默认每 7 天备份一次
- **手动备份**: 管理员可随时触发立即备份
- **智能排除**: 自动排除 `.venv`、`__pycache__`、`.git`、`node_modules` 等目录
- **自动清理**: 可配置保留的备份数量，自动删除旧备份
- **权限控制**: 仅管理员可执行备份操作

## 安装

1. 将插件克隆到 AstrBot 的 `data/plugins` 目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_autobackup.git
```

2. 在 AstrBot WebUI 的插件管理页面，点击"重载插件"

3. 安装依赖：

```bash
pip install croniter
```

## 配置

插件配置文件位于 `data/config/astrbot_plugin_autobackup.json`，可在 AstrBot WebUI 中修改：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `backup_path` | string | "" | 备份保存位置（空字符串表示 AstrBot 的上一级目录） |
| `cron_expression` | string | "0 0 \*/7 * \*" | cron 定时表达式，默认每 7 天备份一次 |
| `max_backups` | int | 5 | 保留的最大备份数量 |

### Cron 表达式说明

格式：`分钟 小时 日期 月份 星期`

示例：
- `0 0 */7 * *` - 每 7 天的 00:00 执行（默认）
- `0 2 * * *` - 每天凌晨 2:00 执行
- `0 0 * * 0` - 每周日凌晨 00:00 执行
- `0 0 1 * *` - 每月 1 日凌晨 00:00 执行

## 使用方法

### 命令

| 命令 | 说明 | 权限要求 |
|------|------|----------|
| `/backup 立即备份` | 立即执行备份操作 | 管理员 |
| `/backup status` | 查看备份文件列表 | 无限制 |

### 示例

```
用户: /backup 立即备份
Bot: 正在执行备份，请稍候...
Bot: 备份成功！
     文件名: astrbot_backup_20250111_143022.zip
     保存位置: /path/to/backup/astrbot_backup_20250111_143022.zip
     大小: 125.34 MB

用户: /backup status
Bot: 备份目录: /path/to/backup
     共 3 个备份文件

     1. astrbot_backup_20250111_143022.zip
        大小: 125.34 MB
        时间: 2025-01-11 14:30:22
     2. astrbot_backup_20250104_000000.zip
        大小: 124.89 MB
        时间: 2025-01-04 00:00:00
     3. astrbot_backup_20241228_000000.zip
        大小: 124.12 MB
        时间: 2024-12-28 00:00:00
```

## 排除的目录和文件

插件会自动排除以下内容：

- **目录**: `.venv`, `__pycache__`, `.git`, `node_modules`
- **文件类型**: `.pyc`, `.log`, `.tmp`

## 备份文件命名

备份文件以 `astrbot_backup_YYYYMMDD_HHMMSS.zip` 格式命名，例如：
- `astrbot_backup_20250111_143022.zip`

## 注意事项

1. 备份操作可能会消耗较多系统资源，建议在低峰期执行
2. 确保 backup_path 配置的目录有足够的磁盘空间
3. 非管理员用户尝试执行备份命令会收到权限不足的提示

## 故障排查

### 备份失败

- 检查磁盘空间是否充足
- 确认 backup_path 路径存在且有写入权限
- 查看日志了解详细错误信息

### 定时备份不执行

- 检查 cron_expression 配置是否正确
- 确认插件已正常加载（查看日志中的插件加载信息）

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
