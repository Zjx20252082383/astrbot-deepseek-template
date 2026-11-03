"""
AstrBot 自动备份插件
自动备份 AstrBot 目录，排除 .venv 文件夹
支持管理员手动触发备份，支持 cron 定时备份
"""

import asyncio
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from croniter import croniter

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger


@register(
    "astrbot_plugin_autobackup",
    "AstrBot Community",
    "AstrBot 自动备份插件，支持定时备份和手动备份，自动排除 .venv 目录",
    "1.0.0",
    "https://github.com/enldm/astrbot_plugin_autobackup"
)
class AutoBackupPlugin(Star):
    """AstrBot 自动备份插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.astrbot_path = self._get_astrbot_path()
        self.backup_task = None
        self._stop_backup = False

    def _get_astrbot_path(self) -> Path:
        """获取 AstrBot 的安装路径"""
        # 基于插件文件位置获取 AstrBot 根目录
        # 插件通常位于 plugins/ 或 plugins/插件名/ 目录下
        plugin_file = Path(__file__).resolve()
        possible_paths = [
            plugin_file.parent.parent,  # plugins/astrbot_plugin_autobackup -> plugins
            plugin_file.parent.parent.parent,  # plugins/astrbot_plugin_autobackup/main.py -> plugins
        ]

        # 尝试找到包含典型 AstrBot 文件的目录
        for path in possible_paths:
            # 检查是否是 AstrBot 根目录（包含典型的文件/文件夹）
            indicators = ['data', 'core', 'plugins']
            if any((path / indicator).exists() for indicator in indicators):
                return path

        # 如果找不到，回退到使用上下文配置路径或当前工作目录
        if hasattr(self.context, 'base_config') and hasattr(self.context.base_config, 'path'):
            return Path(self.context.base_config.path)

        # 最后的回退选项
        return Path.cwd()

    def _get_backup_path(self) -> Path:
        """获取备份保存路径"""
        backup_path = self.config.get("backup_path", "")
        if backup_path and os.path.isabs(backup_path):
            return Path(backup_path)
        # 默认保存到 AstrBot 的上一级目录
        return self.astrbot_path.parent

    def _generate_backup_filename(self) -> str:
        """生成备份文件名"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"astrbot_backup_{timestamp}.zip"

    def _should_exclude(self, path: str, exclude_dirs: list = None) -> bool:
        """判断是否应该排除该路径"""
        if exclude_dirs is None:
            exclude_dirs = [".venv", "__pycache__", ".git", "node_modules"]

        path_obj = Path(path)
        # 检查路径的任何部分是否包含排除的目录名
        for part in path_obj.parts:
            if part in exclude_dirs:
                return True
        return False

    def _create_backup(self) -> dict:
        """执行备份操作"""
        try:
            backup_dir = self._get_backup_path()
            backup_filename = self._generate_backup_filename()
            backup_file = backup_dir / backup_filename

            logger.info(f"开始备份 AstrBot 到: {backup_file}")

            # 确保备份目录存在
            backup_dir.mkdir(parents=True, exist_ok=True)

            # 创建 zip 文件
            with zipfile.ZipFile(
                backup_file, 'w', zipfile.ZIP_DEFLATED, allowZip64=True
            ) as zipf:
                for root, dirs, files in os.walk(self.astrbot_path):
                    # 过滤排除的目录
                    dirs[:] = [d for d in dirs if not self._should_exclude(d)]

                    for file in files:
                        file_path = Path(root) / file
                        # 排除特定的文件类型
                        if file_path.suffix in ['.pyc', '.log', '.tmp']:
                            continue
                        # 排除之前的备份 zip 文件（防止递归备份）
                        if file_path.name.startswith('astrbot_backup_') and file_path.suffix == '.zip':
                            continue

                        arcname = file_path.relative_to(self.astrbot_path)
                        zipf.write(file_path, arcname)

            file_size = backup_file.stat().st_size
            file_size_mb = file_size / (1024 * 1024)

            logger.info(f"备份完成: {backup_file}, 大小: {file_size_mb:.2f} MB")

            # 清理旧备份
            self._cleanup_old_backups(backup_dir)

            return {
                "success": True,
                "path": str(backup_file),
                "size_mb": round(file_size_mb, 2),
                "filename": backup_filename
            }

        except Exception as e:
            logger.error(f"备份失败: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    def _cleanup_old_backups(self, backup_dir: Path):
        """清理旧的备份文件，保留最近 N 个"""
        try:
            max_backups = self.config.get("max_backups", 5)
            if max_backups <= 0:
                return

            # 查找所有备份文件
            backup_files = list(backup_dir.glob("astrbot_backup_*.zip"))

            # 按修改时间排序（最新的在前）
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

            # 删除超过限制数量的旧备份
            if len(backup_files) > max_backups:
                for old_backup in backup_files[max_backups:]:
                    try:
                        old_backup.unlink()
                        logger.info(f"删除旧备份: {old_backup}")
                    except Exception as e:
                        logger.warning(f"删除旧备份失败 {old_backup}: {str(e)}")

        except Exception as e:
            logger.warning(f"清理旧备份时出错: {str(e)}")

    async def _scheduled_backup_task(self):
        """定时备份任务"""
        cron_expr = self.config.get("cron_expression", "0 0 */7 * *")
        logger.info(f"定时备份任务已启动，cron 表达式: {cron_expr}")

        while not self._stop_backup:
            try:
                # 创建 croniter 对象
                cron = croniter(cron_expr, datetime.now())
                next_run = cron.get_next(datetime)

                # 计算等待时间
                wait_seconds = (next_run - datetime.now()).total_seconds()

                if wait_seconds > 0:
                    logger.info(f"下次备份时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    # 等待到下次备份时间，但每分钟检查一次是否需要停止
                    for _ in range(int(wait_seconds / 60)):
                        if self._stop_backup:
                            break
                        await asyncio.sleep(60)
                    if self._stop_backup:
                        break
                    await asyncio.sleep(wait_seconds % 60)

                # 执行备份（在线程池中运行，避免阻塞事件循环）
                logger.info("执行定时备份...")
                result = await asyncio.to_thread(self._create_backup)
                if result["success"]:
                    logger.info(
                        f"定时备份成功: {result['filename']}, "
                        f"大小: {result['size_mb']} MB"
                    )
                else:
                    logger.error(f"定时备份失败: {result.get('error', 'Unknown error')}")

            except Exception as e:
                logger.error(f"定时备份任务出错: {str(e)}")
                # 出错后等待一小时再试
                await asyncio.sleep(3600)

    @filter.command_group("backup")
    async def backup(self):
        """备份相关指令组"""
        pass

    # 检查是否为管理员
    @filter.permission_type(filter.PermissionType.ADMIN)
    @backup.command("立即备份")
    async def manual_backup(self, event: AstrMessageEvent):
        """立即执行备份操作（仅管理员）"""
        yield event.plain_result("正在执行备份，请稍候...")

        # 在线程池中运行备份操作，避免阻塞事件循环
        result = await asyncio.to_thread(self._create_backup)

        if result["success"]:
            yield event.plain_result(
                f"备份成功！\n"
                f"文件名: {result['filename']}\n"
                f"保存位置: {result['path']}\n"
                f"大小: {result['size_mb']} MB"
            )
        else:
            yield event.plain_result(f"备份失败: {result['error']}")

    @backup.command("status")
    async def backup_status(self, event: AstrMessageEvent):
        """查看备份状态"""
        try:
            backup_dir = self._get_backup_path()
            backup_files = list(backup_dir.glob("astrbot_backup_*.zip"))

            if not backup_files:
                yield event.plain_result("暂无备份文件")
                return

            # 按修改时间排序
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

            msg = f"备份目录: {backup_dir}\n"
            msg += f"共 {len(backup_files)} 个备份文件\n\n"

            for i, backup_file in enumerate(backup_files[:5], 1):
                size_mb = backup_file.stat().st_size / (1024 * 1024)
                mtime = datetime.fromtimestamp(backup_file.stat().st_mtime)
                msg += f"{i}. {backup_file.name}\n"
                msg += f"   大小: {size_mb:.2f} MB\n"
                msg += f"   时间: {mtime.strftime('%Y-%m-%d %H:%M:%S')}\n"

            if len(backup_files) > 5:
                msg += f"\n...还有 {len(backup_files) - 5} 个备份文件"

            yield event.plain_result(msg)

        except Exception as e:
            yield event.plain_result(f"查询备份状态失败: {str(e)}")

    async def initialize(self):
        """插件初始化"""
        logger.info("AstrBot 自动备份插件已加载")
        logger.info(f"AstrBot 路径: {self.astrbot_path}")
        logger.info(f"备份保存路径: {self._get_backup_path()}")

        # 启动定时备份任务
        self.backup_task = asyncio.create_task(self._scheduled_backup_task())

    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("正在停止自动备份插件...")
        self._stop_backup = True
        if self.backup_task and not self.backup_task.done():
            self.backup_task.cancel()
            try:
                await self.backup_task
            except asyncio.CancelledError:
                pass
        logger.info("自动备份插件已停止")
