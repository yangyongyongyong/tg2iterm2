"""Telegram 消息路由、模式切换和 iTerm2 流式任务管理。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tarfile
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from adapters.base import InteractiveAdapter, SlashCommand
from adapters.claude_adapter import ClaudeAdapter
from adapters.cursor_adapter import CursorAdapter
from config import AppConfig
from iterm_controller import ITermController
from telegram_client import TelegramBotClient, limit_telegram_text, sanitize_filename
from reminder.manager import ReminderManager
from reminder.models import Reminder
from reminder.parser import ReminderParser
from reminder.handlers import ReminderHandlers
from reminder import ui as reminder_ui

CURSOR_ACTIVE_MARKER = Path("/tmp/tg2iterm2_cursor_active")
CLAUDE_ACTIVE_MARKER = Path("/tmp/tg2iterm2_claude_active")
CURSOR_SESSION_FILE = Path.home() / ".cursor" / "tg2iterm2_session.json"
CLAUDE_SESSION_FILE = Path.home() / ".claude" / "tg2iterm2_session.json"


class SessionMode(Enum):
    """Bot 会话模式。"""

    SHELL = "shell"
    CLAUDE = "claude"
    CURSOR = "cursor"
    REMINDER = "reminder"  # 提醒模式
    REMINDER_CREATE = "reminder_create"  # 创建提醒子模式
    REMINDER_EDIT = "reminder_edit"  # 编辑提醒子模式


HELP_TEXT = """tg2iterm2 已连接。

普通文本会直接发送到当前 iTerm2 tab 并回车执行。
图片会先保存到临时目录，下一条普通文本会自动携带图片路径。
有前台命令运行时，普通文本会作为 stdin 继续输入到该命令。

模式切换:
/claude - 进入 Claude 模式
/cursor - 进入 Cursor 模式
/exit - 退出当前 CLI 模式
/new - 重置当前 CLI 会话

iTerm2 控制 (任何模式下可用):
/tabs - 列出 iTerm2 tab
/use_tab <编号> - 切换默认 tab
/new_tab - 新建 tab
/send <text> - 只输入文本不回车
/enter - 只发送回车键
/ctrl_c - 发送 Ctrl+C
/ctrl_d - 发送 Ctrl+D
/last <n> - 获取倒数 N 行
"""


ITERM_CONTROL_COMMANDS = [
    {"command": "help", "description": "显示帮助"},
    {"command": "claude", "description": "进入 Claude 模式"},
    {"command": "cursor", "description": "进入 Cursor 模式"},
    {"command": "exit", "description": "退出当前 CLI 模式"},
    {"command": "new", "description": "重置当前 CLI 会话"},
    {"command": "tabs", "description": "列出 iTerm2 tab"},
    {"command": "use_tab", "description": "切换默认 tab"},
    {"command": "new_tab", "description": "新建 tab"},
    {"command": "send", "description": "只输入文本不回车"},
    {"command": "enter", "description": "只发送回车键"},
    {"command": "ctrl_c", "description": "发送 Ctrl+C"},
    {"command": "ctrl_d", "description": "发送 Ctrl+D"},
    {"command": "last", "description": "获取倒数 N 行"},
    {"command": "fetch_file_or_dir", "description": "发送文件/图片（目录浏览）"},
    {"command": "send_2_server", "description": "发送文件到服务端"},
    {"command": "stop_receive", "description": "停止接收文件"},
    {"command": "reminder", "description": "进入提醒模式"},
]


class Tg2ITermApp:
    """封装 Telegram Bot 主循环，支持多模式切换。"""

    def __init__(
        self,
        config: AppConfig,
        telegram: TelegramBotClient,
        iterm: ITermController,
    ) -> None:
        """初始化应用对象。"""
        self._config = config
        self._telegram = telegram
        self._iterm = iterm
        self._offset: int | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._command_lock = asyncio.Lock()
        self._image_dir = Path(tempfile.gettempdir()) / "tg2iterm2_images"
        self._pending_image_paths: dict[int, list[str]] = {}
        self._last_image_file_id: dict[int, tuple[str, str, str]] = {}
        self._awaiting_path_input: bool = False
        self._receiving_files: bool = False
        self._receiving_in_progress: int = 0
        _downloads = Path.home() / "Downloads"
        _downloads.mkdir(parents=True, exist_ok=True)
        self._receive_dir: Path = _downloads
        # 权限弹窗
        self._perm_request_path = Path(config.perm_request_path)
        self._perm_response_path = Path(config.perm_response_path)
        self._perm_watcher_task: asyncio.Task[None] | None = None
        self._perm_last_mtime: float = 0.0
        self._perm_active_message_id: int | None = None
        # 模式管理
        self._session_mode = SessionMode.SHELL
        self._claude_adapter = ClaudeAdapter(
            done_signal=config.claude_done_signal,
            hook_timeout=config.claude_hook_timeout,
        )
        self._cursor_adapter = CursorAdapter()
        # 提醒管理
        self._reminder_manager: ReminderManager | None = None
        self._reminder_parser: ReminderParser | None = None
        self._reminder_handlers: ReminderHandlers | None = None
        self._reminder_editing_id: str | None = None  # 当前正在编辑的提醒 ID
        self._pending_reminder_creation: bool = False  # 是否有待处理的提醒创建

    @property
    def _active_adapter(self) -> InteractiveAdapter | None:
        """返回当前活跃的 CLI 适配器。"""
        if self._session_mode == SessionMode.CLAUDE:
            return self._claude_adapter
        if self._session_mode == SessionMode.CURSOR:
            return self._cursor_adapter
        return None

    async def run(self) -> None:
        """启动 Telegram 长轮询主循环和本地 HTTP API。"""
        CURSOR_ACTIVE_MARKER.unlink(missing_ok=True)
        CLAUDE_ACTIVE_MARKER.unlink(missing_ok=True)
        # 初始化提醒管理器
        self._reminder_manager = ReminderManager(
            db_path=Path(self._config.reminder_db_path),
            on_reminder=self._on_reminder_triggered,
        )
        self._reminder_parser = ReminderParser()
        self._reminder_handlers = ReminderHandlers(
            self._telegram,
            self._reminder_manager,
            self._reminder_parser,
        )
        await self._reminder_manager.start()
        async with self._telegram:
            await self._telegram.delete_webhook(drop_pending_updates=True)
            await self._update_bot_menu()
            await self._iterm.connect()
            self._perm_watcher_task = asyncio.create_task(self._watch_permission_requests())
            api_task = asyncio.create_task(self._start_http_api())
            await self._poll_forever()
            api_task.cancel()
            await self._reminder_manager.stop()

    async def _start_http_api(self) -> None:
        """启动本地 HTTP API，支持 curl 发送消息到 Telegram。"""
        from aiohttp import web

        async def handle_send(request: web.Request) -> web.Response:
            """POST /send — 发送文本到 Telegram。

            curl -X POST http://localhost:7288/send -d 'message=hello'
            curl -X POST http://localhost:7288/send -H 'Content-Type: application/json' -d '{"message":"hello"}'
            """
            chat_id = self._config.allowed_chat_id
            content_type = request.content_type
            if content_type == "application/json":
                try:
                    body = await request.json()
                except Exception:
                    return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
                text = body.get("message", "")
            else:
                post = await request.post()
                text = str(post.get("message", ""))

            if not text.strip():
                return web.json_response({"ok": False, "error": "empty message"}, status=400)

            try:
                await self._telegram.send_message(chat_id, text)
                return web.json_response({"ok": True})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=500)

        app = web.Application()
        app.router.add_post("/send", handle_send)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("TG_HTTP_API_PORT", "7288"))
        site = web.TCPSite(runner, "127.0.0.1", port)
        print(f"HTTP API 监听 http://127.0.0.1:{port}/send")
        await site.start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    async def _update_bot_menu(self) -> None:
        """根据当前模式动态更新 Bot 菜单。

        控制命令在上方，CLI 的 skill 按名称排序放在底部。
        """
        commands = list(ITERM_CONTROL_COMMANDS)
        
        # 根据当前模式添加特定命令
        if self._session_mode == SessionMode.REMINDER:
            commands.append({"command": "exit_reminder", "description": "退出提醒模式"})
        
        adapter = self._active_adapter
        if adapter is not None:
            slash_cmds = adapter.get_slash_commands()
            for cmd in slash_cmds:
                tg_name = _slash_to_tg_command(cmd.name)
                desc = cmd.description
                if len(desc) > 200:
                    desc = desc[:197] + "..."
                commands.append({"command": tg_name, "description": f"[{adapter.name}] {desc}"})
        # Telegram Bot 菜单最多 100 条命令
        if len(commands) > 100:
            commands = commands[:13] + commands[13:][:87]
        await self._telegram.set_my_commands(commands)
        mode_label = self._session_mode.value.upper()
        print(f"Bot 菜单已更新 (模式: {mode_label}, 命令数: {len(commands)})")

    async def _poll_forever(self) -> None:
        """持续拉取 Telegram 更新。"""
        consecutive_errors = 0
        while True:
            try:
                updates = await self._telegram.get_updates(self._offset)
                consecutive_errors = 0
                for update in updates:
                    self._offset = int(update["update_id"]) + 1
                    asyncio.create_task(self._handle_update(update))
            except Exception as exc:
                consecutive_errors += 1
                wait = min(consecutive_errors * 5, 60)
                print(f"轮询 Telegram 失败 (第{consecutive_errors}次): {exc}")
                print(f"等待 {wait}s 后重试")
                await asyncio.sleep(wait)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """处理单条 Telegram update。"""
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        text = message.get("text")

        if chat_id != self._config.allowed_chat_id or chat_type != "private":
            if chat_id is not None:
                await self._telegram.send_message(int(chat_id), "Forbidden")
            return
        # ─── 文件接收模式：保存到本机 ───
        if self._receiving_files and (self._has_image(message) or message.get("document")):
            await self._receive_file_to_server(chat_id, message)
            return
        if self._has_image(message):
            self._remember_image(int(chat_id), message)
            text = message.get("text") or message.get("caption") or ""
            if not text.strip():
                await self._telegram.send_message(chat_id, "图片已记录，reply 引用该图片并附带文本即可发给 CLI")
                return
            # 图片+文字一起发：立即下载并携带
            path = await self._download_image(int(chat_id), message)
            if path:
                text = f"{path} {text}"
            await self._dispatch_text(chat_id, text)
            return
        # 用户 reply 了一条图片消息，下载被引用的图片并携带
        reply_msg = message.get("reply_to_message") or {}
        if self._has_image(reply_msg) and isinstance(text, str) and text.strip():
            path = await self._download_image(int(chat_id), reply_msg)
            if path:
                text = f"{path} {text}"
        if not isinstance(text, str) or not text.strip():
            await self._telegram.send_message(chat_id, "仅支持文本或图片消息")
            return
        await self._dispatch_text(chat_id, text)

    async def _dispatch_text(self, chat_id: int, text: str) -> None:
        """按模式和控制命令分发文本。"""
        stripped = text.strip()

        # ─── 等待路径输入 ───
        if self._awaiting_path_input:
            self._awaiting_path_input = False
            p = Path(stripped)
            if not p.exists():
                await self._telegram.send_message(chat_id, f"路径不存在: {stripped}")
                return
            if p.is_dir():
                await self._telegram.send_message(chat_id, f"\U0001f4c1 识别为目录，正在打包 {p.name}...")
                try:
                    archive = _tar_gz_directory(p, _FB_SEND_DIR)
                    await self._send_local_path(chat_id, archive)
                finally:
                    archive_path = _FB_SEND_DIR / f"{p.name}.tar.gz"
                    archive_path.unlink(missing_ok=True)
            elif _is_image_file(p):
                await self._telegram.send_message(chat_id, f"\U0001f5bc 识别为图片，发送中...")
                await self._send_local_path(chat_id, p)
            else:
                size_str = _format_file_size(p.stat().st_size)
                await self._telegram.send_message(chat_id, f"\U0001f4c4 识别为文件 ({size_str})，发送中...")
                await self._send_local_path(chat_id, p)
            return

        # ─── 全局控制命令（任何模式下可用） ───
        if stripped in ("/start", "/help"):
            await self._telegram.send_message(chat_id, HELP_TEXT)
            return
        if stripped == "/tabs":
            await self._send_tabs(chat_id)
            return
        if stripped.startswith("/use_tab "):
            await self._use_tab(chat_id, stripped.split(maxsplit=1)[1])
            return
        if stripped == "/new_tab":
            await self._new_tab(chat_id)
            return
        if stripped.startswith("/send "):
            await self._send_raw_text(chat_id, text.split(" ", 1)[1])
            return
        if stripped == "/enter":
            await self._enter(chat_id)
            return
        if stripped == "/ctrl_c":
            await self._ctrl_c(chat_id)
            return
        if stripped == "/ctrl_d":
            await self._ctrl_d(chat_id)
            return
        if stripped.startswith("/last "):
            await self._last_lines(chat_id, stripped.split(maxsplit=1)[1])
            return
        if stripped == "/get_last_10_line":
            await self._send_last_lines(chat_id, 10)
            return
        if stripped == "/fetch_file_or_dir":
            await self._handle_fetch_file_or_dir(chat_id)
            return
        if stripped == "/send_2_server":
            self._receiving_files = True
            await self._telegram.send_message(
                chat_id,
                f"已进入文件接收模式，发送文件/图片将保存到 {self._receive_dir}\n"
                f"发送 /stop_receive 结束接收",
            )
            return
        if stripped == "/stop_receive":
            self._receiving_files = False
            if self._receiving_in_progress > 0:
                await self._telegram.send_message(
                    chat_id,
                    f"等待 {self._receiving_in_progress} 个文件传输完成...",
                )
                while self._receiving_in_progress > 0:
                    await asyncio.sleep(0.5)
            await self._telegram.send_message(chat_id, "已退出文件接收模式")
            return

        # ─── 模式切换命令 ───
        if stripped == "/claude" or stripped.startswith("/claude "):
            prompt = stripped[7:].strip() if len(stripped) > 7 else ""
            await self._enter_cli_mode(chat_id, SessionMode.CLAUDE, prompt)
            return
        if stripped == "/cursor" or stripped.startswith("/cursor "):
            prompt = stripped[8:].strip() if len(stripped) > 8 else ""
            await self._enter_cli_mode(chat_id, SessionMode.CURSOR, prompt)
            return
        if stripped == "/reminder":
            await self._enter_reminder_mode(chat_id)
            return
        if stripped == "/exit_reminder":
            await self._exit_reminder_mode(chat_id)
            return

        # ─── 提醒模式处理 ───
        if self._session_mode == SessionMode.REMINDER_CREATE:
            await self._handle_reminder_create_input(chat_id, stripped)
            return
        if self._session_mode == SessionMode.REMINDER_EDIT:
            await self._handle_reminder_edit_input(chat_id, stripped)
            return
        if self._session_mode == SessionMode.REMINDER:
            # 提醒模式下，文本命令处理
            await self._handle_reminder_mode_input(chat_id, stripped)
            return
        if stripped == "/exit":
            if self._session_mode in (SessionMode.REMINDER, SessionMode.REMINDER_CREATE, SessionMode.REMINDER_EDIT):
                await self._exit_reminder_mode(chat_id)
            else:
                await self._exit_cli_mode(chat_id)
            return
        if stripped == "/new":
            await self._reset_cli_session(chat_id)
            return

        # ─── 按当前模式路由 ───
        if self._session_mode in (SessionMode.CLAUDE, SessionMode.CURSOR):
            await self._send_to_cli(chat_id, self._consume_image_paths(chat_id, text))
            return

        # ─── Shell 模式：普通终端命令 ───
        await self._start_terminal_command(chat_id, self._consume_image_paths(chat_id, text))

    # ─── 模式管理 ───

    async def _enter_cli_mode(self, chat_id: int, mode: SessionMode, initial_prompt: str) -> None:
        """进入 CLI 模式（Claude 或 Cursor）。

        通过 run_command_stream 在 iTerm2 中启动 CLI 命令，
        这样 iTerm controller 能正确跟踪前台命令状态。
        """
        if self._session_mode == mode:
            await self._telegram.send_message(chat_id, f"已在 {mode.value} 模式中")
            if initial_prompt:
                await self._send_to_cli(chat_id, initial_prompt)
            return

        if self._session_mode != SessionMode.SHELL:
            await self._exit_cli_mode(chat_id, silent=True)

        self._session_mode = mode
        adapter = self._active_adapter
        assert adapter is not None
        mode_label = adapter.name.capitalize()

        # 从持久化文件读取上次的 session ID
        saved_session_id = _read_session_id(mode)
        cli_cmd = adapter.get_launch_command(session_id=saved_session_id)

        if saved_session_id:
            status_msg = f"已进入 {mode_label} 模式\n正在恢复会话 {saved_session_id[:8]}...\n发送文本与 {mode_label} 对话，/exit 退出\n/new 可开启全新会话"
        else:
            status_msg = f"已进入 {mode_label} 模式\n正在启动新会话...\n发送文本与 {mode_label} 对话，/exit 退出"

        await self._telegram.send_message(chat_id, status_msg)

        # 创建标记文件，让 hook 脚本知道 bot 处于活跃的 CLI 模式
        _set_active_marker(mode, active=True)

        # 通过 run_command_stream 启动 CLI，这样 iterm_controller 能跟踪前台状态
        async def on_cli_update(output: str) -> None:
            """CLI 启动期间的输出回调（忽略）。"""
            pass

        async with self._command_lock:
            self._command_task = asyncio.create_task(
                self._iterm.run_command_stream(
                    command=cli_cmd,
                    on_update=on_cli_update,
                    stream_interval=self._config.stream_interval,
                )
            )
            self._command_task.add_done_callback(self._clear_command_task)

        await self._update_bot_menu()

        if initial_prompt:
            await asyncio.sleep(2.0)
            await self._send_to_cli(chat_id, initial_prompt)

    async def _exit_cli_mode(self, chat_id: int, silent: bool = False) -> None:
        """退出当前 CLI 模式，回到 Shell。

        会向 iTerm2 发送 Ctrl+C 终止正在运行的 CLI 进程。
        """
        if self._session_mode == SessionMode.SHELL:
            if not silent:
                await self._telegram.send_message(chat_id, "当前已是 Shell 模式")
            return

        old_mode = self._session_mode.value.capitalize()
        _set_active_marker(self._session_mode, active=False)
        self._session_mode = SessionMode.SHELL
        self._perm_active_message_id = None

        # 向 iTerm2 发送退出序列关闭 CLI
        # Cursor/Claude CLI 通常需要多次 Ctrl+C 或特定命令才能退出
        try:
            session = await self._iterm.get_target_session()
            # 先发 Escape（取消可能的输入状态）
            await session.async_send_text("\x1b", suppress_broadcast=True)
            await asyncio.sleep(0.2)
            # 连续两次 Ctrl+C
            await session.async_send_text("\x03", suppress_broadcast=True)
            await asyncio.sleep(0.3)
            await session.async_send_text("\x03", suppress_broadcast=True)
        except Exception:
            pass

        if not silent:
            await self._telegram.send_message(chat_id, f"已退出 {old_mode} 模式，回到 Shell")
        await self._update_bot_menu()

    async def _reset_cli_session(self, chat_id: int) -> None:
        """在当前 CLI 模式下重置会话（Ctrl+C 终止后重新启动 CLI）。"""
        if self._session_mode == SessionMode.SHELL:
            await self._telegram.send_message(chat_id, "Shell 模式无需重置")
            return
        adapter = self._active_adapter
        assert adapter is not None
        mode_label = adapter.name.capitalize()

        # 退出当前 CLI
        try:
            session = await self._iterm.get_target_session()
            await session.async_send_text("\x1b", suppress_broadcast=True)
            await asyncio.sleep(0.2)
            await session.async_send_text("\x03", suppress_broadcast=True)
            await asyncio.sleep(0.3)
            await session.async_send_text("\x03", suppress_broadcast=True)
        except Exception:
            pass

        await asyncio.sleep(1.0)

        # 清除持久化的 session ID 和标记文件
        _clear_session_id(self._session_mode)
        _set_active_marker(self._session_mode, active=True)

        # 重新启动 CLI（不带 --resume，全新会话）
        try:
            new_cmd = adapter.get_launch_command(session_id=None)
            await self._iterm.send_text(new_cmd, enter=True)
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"重启 {mode_label} 失败: {exc}")
            return

        await self._telegram.send_message(chat_id, f"已重置 {mode_label} 会话（新会话将在首次交互后绑定）")

    # ─── 提醒模式方法 ───

    async def _on_reminder_triggered(self, reminder: Reminder) -> None:
        """提醒触发时的回调。"""
        print(f"[BOT回调] 触发提醒: {reminder.id} - {reminder.content} -> chat_id={reminder.chat_id}")
        await self._reminder_handlers.on_reminder_triggered(reminder)
        print(f"[BOT回调] 消息已发送")

    async def _enter_reminder_mode(self, chat_id: int) -> None:
        """进入提醒模式。"""
        if self._session_mode in (SessionMode.REMINDER, SessionMode.REMINDER_CREATE, SessionMode.REMINDER_EDIT):
            await self._reminder_handlers.send_reminder_menu(chat_id)
            return

        if self._session_mode != SessionMode.SHELL:
            await self._exit_cli_mode(chat_id, silent=True)

        self._session_mode = SessionMode.REMINDER
        await self._telegram.send_message(chat_id, "已进入提醒模式")
        await self._reminder_handlers.send_reminder_menu(chat_id)

    async def _exit_reminder_mode(self, chat_id: int) -> None:
        """退出提醒模式。"""
        self._session_mode = SessionMode.SHELL
        self._reminder_editing_id = None
        await self._telegram.send_message(chat_id, "已退出提醒模式")

    async def _handle_reminder_mode_input(self, chat_id: int, text: str) -> None:
        """处理提醒模式下的文本输入。"""
        if text == "/exit":
            await self._exit_reminder_mode(chat_id)
            return
        # 其他文本在提醒模式下忽略
        await self._telegram.send_message(chat_id, "请使用菜单按钮操作，或发送 /exit 退出")

    async def _handle_reminder_create_input(self, chat_id: int, text: str) -> None:
        """处理创建提醒时的文本输入 - 直接调用 Cursor CLI 解析。"""
        if text == "/exit":
            self._session_mode = SessionMode.REMINDER
            await self._reminder_handlers.send_reminder_menu(chat_id)
            return

        await self._telegram.send_message(
            chat_id,
            "正在通过 Cursor CLI 解析您的提醒请求...",
        )

        # 直接调用 Cursor CLI（后台静默执行）
        result = await self._reminder_parser.parse_and_create(text, chat_id)

        if result.get("success"):
            output = result.get("output", "")
            # 检查是否成功创建了提醒
            if "成功" in output or "已创建" in output or "reminder_id" in output.lower() or "reminder" in output.lower():
                # 重新从数据库加载提醒（同步外部创建的提醒）
                await self._reminder_manager.reload_reminders()
                await self._telegram.send_message(
                    chat_id,
                    f"✅ 提醒创建成功\n\n{output}",
                )
                # 显示提醒列表
                await self._reminder_handlers.send_reminder_list(chat_id)
            else:
                await self._telegram.send_message(
                    chat_id,
                    f"解析结果：\n{output}",
                )
        else:
            error = result.get("error", "未知错误")
            await self._telegram.send_message(
                chat_id,
                f"❌ 解析失败：{error}",
            )
            # 提示用户重新输入或退出
            await self._telegram.send_message(
                chat_id,
                "请重新描述您的提醒，或发送 /exit 退出",
            )

    async def _handle_reminder_edit_input(self, chat_id: int, text: str) -> None:
        """处理编辑提醒时的文本输入。"""
        if text == "/exit":
            self._session_mode = SessionMode.REMINDER
            self._reminder_editing_id = None
            await self._reminder_handlers.send_reminder_menu(chat_id)
            return

        if not self._reminder_editing_id:
            await self._telegram.send_message(chat_id, "编辑会话已失效")
            self._session_mode = SessionMode.REMINDER
            return

        # 根据编辑类型处理（内容或时间）
        reminder = self._reminder_manager.get_reminder(self._reminder_editing_id)
        if not reminder:
            await self._telegram.send_message(chat_id, "提醒不存在")
            self._session_mode = SessionMode.REMINDER
            return

        success = await self._reminder_handlers.handle_edit_content(chat_id, self._reminder_editing_id, text)
        if success:
            self._session_mode = SessionMode.REMINDER
            self._reminder_editing_id = None
            await self._reminder_handlers.send_reminder_detail(chat_id, self._reminder_editing_id)

    async def _handle_reminder_callback(
        self, callback_id: str, data: str, chat_id: int, message_id: int
    ) -> None:
        """处理提醒相关的回调。"""
        parts = data.split("_")
        action = parts[1] if len(parts) > 1 else ""

        if action == "menu":
            await self._reminder_handlers.send_reminder_menu(chat_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "create":
            self._session_mode = SessionMode.REMINDER_CREATE
            await self._reminder_handlers.send_create_prompt(chat_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "list":
            await self._reminder_handlers.send_reminder_list(chat_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "exit":
            await self._exit_reminder_mode(chat_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "detail" and len(parts) > 2:
            reminder_id = parts[2]
            await self._reminder_handlers.send_reminder_detail(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "pause" and len(parts) > 2:
            reminder_id = parts[2]
            await self._reminder_handlers.handle_pause(chat_id, reminder_id)
            await self._reminder_handlers.send_reminder_detail(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id, "已暂停")
            return

        if action == "resume" and len(parts) > 2:
            reminder_id = parts[2]
            await self._reminder_handlers.handle_resume(chat_id, reminder_id)
            await self._reminder_handlers.send_reminder_detail(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id, "已恢复")
            return

        if action == "delete" and len(parts) > 2:
            reminder_id = parts[2]
            await self._reminder_handlers.send_delete_confirm(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        if action == "delete" and parts[2] == "confirm" and len(parts) > 3:
            reminder_id = parts[3]
            await self._reminder_handlers.handle_delete(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id, "已删除")
            return

        if action == "edit" and len(parts) > 2:
            reminder_id = parts[2]
            edit_type = parts[3] if len(parts) > 3 else ""

            if edit_type == "content":
                self._session_mode = SessionMode.REMINDER_EDIT
                self._reminder_editing_id = reminder_id
                await self._telegram.send_message(chat_id, "请输入新的提醒内容：")
                await self._telegram.answer_callback_query(callback_id)
                return

            if edit_type == "time":
                self._session_mode = SessionMode.REMINDER_EDIT
                self._reminder_editing_id = reminder_id
                await self._telegram.send_message(
                    chat_id,
                    "请输入新的时间，例如：\n"
                    "- 每周三 20:00\n"
                    "- 每天 22:00\n"
                    "- 2026-05-15 10:00",
                )
                await self._telegram.answer_callback_query(callback_id)
                return

            await self._reminder_handlers.send_edit_menu(chat_id, reminder_id)
            await self._telegram.answer_callback_query(callback_id)
            return

        await self._telegram.answer_callback_query(callback_id)

    async def _send_final_output(self, chat_id: int, message_id: int, text: str) -> None:
        """发送最终完成输出：短文本直接编辑，长文本分片追加新消息；自动发送引用的图片。"""
        if len(text) <= 4000:
            await self._telegram.edit_markdown_message(chat_id, message_id, text)
        else:
            await self._telegram.edit_message_text(chat_id, message_id, "✅ 已完成（见下方完整输出）")
            await self._telegram.send_markdown_message(chat_id, text)

        for path in _extract_image_paths(text):
            try:
                await self._telegram.send_photo(chat_id, path)
            except Exception as exc:
                print(f"发送图片失败 {path}: {exc}")

    async def _send_to_cli(self, chat_id: int, text: str) -> None:
        """向当前活跃的 CLI 发送消息（通过 iTerm2 前台 stdin 输入）。

        统一走 send_foreground_input_stream，iterm_controller 内部
        会根据 CLI 类型决定文本/回车的发送方式和回合完成检测逻辑。
        """
        message = await self._telegram.send_message(chat_id, "输入中...")
        message_id = int(message["message_id"])
        last_rendered = ""

        async def on_update(output: str) -> None:
            """把本次交互输出更新到 Telegram 消息。"""
            nonlocal last_rendered
            rendered = render_stream_message(text, output, finished=False)
            if rendered != last_rendered:
                await self._telegram.edit_markdown_message(chat_id, message_id, rendered)
                last_rendered = rendered

        try:
            output = await self._iterm.send_foreground_input_stream(
                text=text,
                on_update=on_update,
                stream_interval=self._config.stream_interval,
            )
            final = render_stream_message(text, output, finished=True)
            await self._send_final_output(chat_id, message_id, final)
        except Exception as exc:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                f"输入失败: {exc}",
            )

    # ─── 终端命令执行 ───

    async def _start_terminal_command(self, chat_id: int, command: str) -> None:
        """启动普通终端命令，或把文本送入正在运行的交互式命令。"""
        async with self._command_lock:
            if self._command_task is not None and not self._command_task.done():
                if self._iterm._foreground_command_name == "claude":
                    await self._telegram.send_message(
                        chat_id,
                        "⚠️ 上一个命令尚未完成，请等待执行结束后再发送新命令。",
                    )
                    return
                await self._run_foreground_input(chat_id, command)
                return
            self._command_task = asyncio.create_task(
                self._run_terminal_command(chat_id, command)
            )
            self._command_task.add_done_callback(self._clear_command_task)

    def _clear_command_task(self, task: asyncio.Task[None]) -> None:
        """命令任务结束后清理任务引用。"""
        if self._command_task is task:
            self._command_task = None

    async def _run_terminal_command(self, chat_id: int, command: str) -> None:
        """执行终端命令，并把屏幕变化流式编辑到 Telegram。"""
        message = await self._telegram.send_message(chat_id, "执行中...")
        message_id = int(message["message_id"])
        last_rendered = ""

        async def on_update(output: str) -> None:
            """把终端输出更新到 Telegram 消息。"""
            nonlocal last_rendered
            rendered = render_stream_message(command, output, finished=False)
            if rendered != last_rendered:
                await self._telegram.edit_markdown_message(chat_id, message_id, rendered)
                last_rendered = rendered

        try:
            result = await self._iterm.run_command_stream(
                command=command,
                on_update=on_update,
                stream_interval=self._config.stream_interval,
            )
            final_text = render_stream_message(
                command,
                result.output,
                finished=True,
                exit_status=result.exit_status,
            )
            await self._send_final_output(chat_id, message_id, final_text)
        except Exception as exc:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                f"执行失败: {exc}",
            )

    async def _run_foreground_input(self, chat_id: int, text: str) -> None:
        """把普通文本送入正在运行的前台命令，并返回本次新增输出。"""
        message = await self._telegram.send_message(chat_id, "输入中...")
        message_id = int(message["message_id"])
        last_rendered = ""

        async def on_update(output: str) -> None:
            """把本次交互输出更新到 Telegram 消息。"""
            nonlocal last_rendered
            rendered = render_stream_message(text, output, finished=False)
            if rendered != last_rendered:
                await self._telegram.edit_markdown_message(chat_id, message_id, rendered)
                last_rendered = rendered

        try:
            output = await self._iterm.send_foreground_input_stream(
                text=text,
                on_update=on_update,
                stream_interval=self._config.stream_interval,
            )
            final = render_stream_message(text, output, finished=True)
            await self._send_final_output(chat_id, message_id, final)
        except Exception as exc:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                f"输入失败: {exc}",
            )

    # ─── 权限弹窗（Claude 和 Cursor 共用） ───

    async def _watch_permission_requests(self) -> None:
        """轮询权限请求：hook 文件 + 屏幕选项检测。

        两种触发方式：
        1. hook 脚本写入请求文件（mtime 变化）
        2. iTerm2 屏幕上出现选项（❯ 或 →）且无活跃按钮消息
        """
        if self._perm_request_path.exists():
            self._perm_last_mtime = self._perm_request_path.stat().st_mtime
        while True:
            try:
                # 检查 hook 文件触发（仅在 CLI 模式下才处理）
                if self._session_mode != SessionMode.SHELL and self._perm_request_path.exists():
                    current_mtime = self._perm_request_path.stat().st_mtime
                    if current_mtime > self._perm_last_mtime:
                        self._perm_last_mtime = current_mtime
                        await self._send_permission_keyboard()

                # 定期检查标记文件中 hook 是否绑定了新的 conversation_id，持久化
                if self._session_mode != SessionMode.SHELL:
                    _sync_session_id_from_marker(self._session_mode)

                # 在 CLI 模式下，主动检查屏幕是否有选项出现
                if (
                    self._perm_active_message_id is None
                    and self._session_mode != SessionMode.SHELL
                ):
                    await self._check_screen_options()

                # 如果有活跃按钮消息，检查屏幕选项是否已消失
                if self._perm_active_message_id is not None:
                    await self._check_permission_dismissed()
            except Exception as exc:
                print(f"权限监控异常: {exc}")
            await asyncio.sleep(self._config.perm_poll_interval)

    async def _check_screen_options(self) -> None:
        """主动检查 iTerm2 屏幕是否出现了权限选项（不依赖 hook 文件）。"""
        try:
            session = await self._iterm.get_target_session()
            screen_text = await self._iterm.read_session_screen_text(session)
        except Exception:
            return

        options = parse_selection_options(screen_text)
        if options:
            await self._send_screen_permission_keyboard()

    async def _send_permission_keyboard(self) -> None:
        """根据请求来源（Claude/Cursor），动态生成 Inline Keyboard。

        支持两种触发方式：
        1. hook 脚本写入请求文件（Cursor preToolUse）→ 生成 allow/deny 按钮
        2. 屏幕上出现选项（Claude ❯ 或 Cursor →）→ 从屏幕解析生成选项按钮
        """
        request_data = {}
        try:
            request_data = json.loads(self._perm_request_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

        source = request_data.get("source", "claude")

        if source == "cursor":
            await self._send_cursor_permission_keyboard(request_data)
        else:
            await self._send_screen_permission_keyboard()

    async def _send_screen_permission_keyboard(self) -> None:
        """从 iTerm2 屏幕读取选项（Claude 或 Cursor 格式），动态生成按钮。"""
        try:
            session = await self._iterm.get_target_session()
            screen_text = await self._iterm.read_session_screen_text(session)
        except Exception:
            screen_text = ""

        options = parse_selection_options(screen_text)
        if not options:
            return

        question = extract_question_text(screen_text)
        mode_label = self._session_mode.value.capitalize() if self._session_mode != SessionMode.SHELL else "终端"
        text = f"🔐 {mode_label} 交互\n\n{question}" if question else f"🔐 {mode_label} 请求确认"

        inline_keyboard = []
        for idx, (number, option_text) in enumerate(options):
            button_text = f"{number}. {option_text}"
            if len(button_text) > 64:
                button_text = button_text[:61] + "..."
            inline_keyboard.append(
                [{"text": button_text, "callback_data": f"perm:{idx}"}]
            )

        reply_markup = {"inline_keyboard": inline_keyboard}
        result = await self._telegram.send_message_with_reply_markup(
            self._config.allowed_chat_id,
            text,
            reply_markup,
        )
        self._perm_active_message_id = int(result["message_id"])

    async def _send_cursor_permission_keyboard(self, request_data: dict) -> None:
        """根据 Cursor hook 请求数据生成按钮。"""
        tool_name = request_data.get("tool_name", "Unknown")
        tool_input = request_data.get("tool_input", {})
        description = request_data.get("description", "")

        command_text = ""
        if isinstance(tool_input, dict):
            command_text = tool_input.get("command", "")

        text = f"🔐 Cursor 请求确认\n\n工具: {tool_name}"
        if command_text:
            text += f"\n命令: {command_text}"
        elif description:
            text += f"\n{description}"

        inline_keyboard = [
            [{"text": "✅ 允许", "callback_data": "perm_cursor:allow"}],
            [{"text": "❌ 拒绝", "callback_data": "perm_cursor:deny"}],
        ]

        reply_markup = {"inline_keyboard": inline_keyboard}
        result = await self._telegram.send_message_with_reply_markup(
            self._config.allowed_chat_id,
            limit_telegram_text(text),
            reply_markup,
        )
        self._perm_active_message_id = int(result["message_id"])

    async def _check_permission_dismissed(self) -> None:
        """检查屏幕上的选项是否已消失，如果消失则撤销 TG 按钮并释放 hook。"""
        try:
            session = await self._iterm.get_target_session()
            screen_text = await self._iterm.read_session_screen_text(session)
        except Exception:
            return

        options = parse_selection_options(screen_text)
        if not options:
            if not self._perm_response_path.exists():
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "权限提示已消失，自动放行",
                    }
                }
                self._perm_response_path.write_text(json.dumps(response))
            await self._dismiss_permission_keyboard("（已在终端中处理）")

    async def _dismiss_permission_keyboard(self, reason: str) -> None:
        """移除活跃的权限按钮消息。"""
        if self._perm_active_message_id is None:
            return
        try:
            await self._telegram.edit_message_text(
                self._config.allowed_chat_id,
                self._perm_active_message_id,
                reason,
            )
        except Exception:
            pass
        self._perm_active_message_id = None

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        """处理 Inline Keyboard 按钮点击。"""
        callback_id = str(callback_query.get("id", ""))
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if chat_id != self._config.allowed_chat_id:
            await self._telegram.answer_callback_query(callback_id, "无权限")
            return

        # 提醒相关回调
        if data.startswith("reminder_"):
            await self._handle_reminder_callback(callback_id, data, chat_id, message_id)
            return

        if data.startswith("perm_cursor:"):
            await self._handle_cursor_perm_callback(callback_id, data, message, chat_id, message_id)
            return

        if data.startswith("perm:"):
            await self._handle_screen_perm_callback(callback_id, data, message, chat_id, message_id)
            return

        if data.startswith("fb:"):
            await self._handle_filebrowser_callback(callback_id, data, chat_id, message_id)
            return

        await self._telegram.answer_callback_query(callback_id)

    async def _handle_cursor_perm_callback(
        self, callback_id: str, data: str, message: dict, chat_id: int, message_id: int
    ) -> None:
        """处理 Cursor 权限按钮点击。"""
        decision = data.split(":", 1)[1]
        response = {"permission": decision}
        self._perm_response_path.write_text(json.dumps(response))

        label = "✅ 已允许" if decision == "allow" else "❌ 已拒绝"
        await self._telegram.answer_callback_query(callback_id, label)
        original_text = message.get("text", "")
        if message_id:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                f"{original_text}\n\n{label}",
            )
        self._perm_active_message_id = None

    async def _handle_screen_perm_callback(
        self, callback_id: str, data: str, message: dict, chat_id: int, message_id: int
    ) -> None:
        """处理屏幕选项按钮点击，向 iTerm2 发送对应位置的按键。

        适用于 Claude（❯ 编号选项）和 Cursor（→ 箭头选项）的屏幕权限弹窗。
        """
        try:
            option_idx = int(data.split(":", 1)[1])
        except ValueError:
            await self._telegram.answer_callback_query(callback_id, "无效选项")
            return

        DOWN_ARROW = "\x1b[B"
        ENTER = "\r"

        try:
            session = await self._iterm.get_target_session()
            for _ in range(option_idx):
                await session.async_send_text(DOWN_ARROW, suppress_broadcast=True)
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.1)
            await session.async_send_text(ENTER, suppress_broadcast=True)
        except Exception as exc:
            print(f"发送按键到 iTerm2 失败: {exc}")

        button_text = ""
        original_text = message.get("text", "")
        inline_kb = message.get("reply_markup", {}).get("inline_keyboard", [])
        if 0 <= option_idx < len(inline_kb) and inline_kb[option_idx]:
            button_text = inline_kb[option_idx][0].get("text", "")
        label = f"→ 已选择: {button_text}" if button_text else f"→ 已选择选项 {option_idx + 1}"

        is_deny = False
        if button_text:
            lower_btn = button_text.lower()
            is_deny = any(kw in lower_btn for kw in ("no", "deny", "拒绝", "reject"))
        decision = "deny" if is_deny else "allow"
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": decision,
                "permissionDecisionReason": f"用户通过 Telegram 选择了选项 {option_idx + 1}",
            }
        }
        self._perm_response_path.write_text(json.dumps(response))

        await self._telegram.answer_callback_query(callback_id, label[:200])
        if message_id:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                f"{original_text}\n\n{label}",
            )
        self._perm_active_message_id = None

    # ─── 图片处理 ───

    def _has_image(self, message: dict[str, Any]) -> bool:
        """判断 Telegram 消息是否包含图片。"""
        document = message.get("document") or {}
        mime_type = str(document.get("mime_type") or "")
        return bool(message.get("photo")) or mime_type.startswith("image/")

    async def _download_image(
        self,
        chat_id: int,
        message: dict[str, Any],
    ) -> str | None:
        """下载图片并返回本地路径。"""
        try:
            file_id, unique_id, default_suffix = self._select_image_file(message)
            prefix = f"chat_{chat_id}_msg_{message.get('message_id', 'unknown')}_{unique_id}"
            path = await self._telegram.download_file_by_id(
                file_id=file_id,
                directory=self._image_dir,
                filename_prefix=prefix,
                default_suffix=default_suffix,
            )
            return str(path)
        except Exception as exc:
            print(f"下载 Telegram 图片失败: {exc}")
            return None

    def _remember_image(self, chat_id: int, message: dict[str, Any]) -> None:
        """记录图片的 file_id 信息，供后续 reply 时按需下载。"""
        try:
            file_id, unique_id, suffix = self._select_image_file(message)
            self._last_image_file_id[chat_id] = (file_id, unique_id, suffix)
        except Exception:
            pass

    def _select_image_file(self, message: dict[str, Any]) -> tuple[str, str, str]:
        """从 Telegram 消息里选择要下载的图片文件。"""
        photos = message.get("photo") or []
        if photos:
            photo = max(photos, key=lambda item: int(item.get("file_size") or 0))
            return (
                str(photo["file_id"]),
                str(photo.get("file_unique_id") or photo["file_id"]),
                ".jpg",
            )

        document = message.get("document") or {}
        file_id = str(document["file_id"])
        unique_id = str(document.get("file_unique_id") or file_id)
        file_name = str(document.get("file_name") or "")
        suffix = Path(file_name).suffix or ".jpg"
        return file_id, unique_id, suffix

    def _consume_image_paths(self, chat_id: int, text: str) -> str:
        """取出挂起图片路径，并把路径前置到普通文本。"""
        paths = self._pending_image_paths.pop(chat_id, [])
        if not paths:
            return text
        prefix = " ".join(paths)
        return f"{prefix} {text}"

    async def _receive_file_to_server(self, chat_id: int, message: dict[str, Any]) -> None:
        """接收用户发送的文件/图片，保存到本机下载目录。"""
        self._receiving_in_progress += 1
        try:
            await self._do_receive_file(chat_id, message)
        finally:
            self._receiving_in_progress -= 1

    async def _do_receive_file(self, chat_id: int, message: dict[str, Any]) -> None:
        """实际执行文件下载和保存。"""
        document = message.get("document") or {}
        photos = message.get("photo") or []

        if document.get("file_id"):
            file_id = str(document["file_id"])
            file_name = str(document.get("file_name") or f"file_{message.get('message_id', 'unknown')}")
            default_suffix = Path(file_name).suffix or ""
        elif photos:
            photo = max(photos, key=lambda item: int(item.get("file_size") or 0))
            file_id = str(photo["file_id"])
            file_name = f"photo_{message.get('message_id', 'unknown')}.jpg"
            default_suffix = ".jpg"
        else:
            await self._telegram.send_message(chat_id, "无法识别文件")
            return

        try:
            file_info = await self._telegram._request("getFile", {"file_id": file_id})
            remote_path = str(file_info["file_path"])
            suffix = Path(remote_path).suffix or default_suffix
            safe_name = sanitize_filename(Path(file_name).stem) + suffix
            dest = self._receive_dir / safe_name
            counter = 1
            while dest.exists():
                dest = self._receive_dir / f"{Path(safe_name).stem}_{counter}{suffix}"
                counter += 1

            await self._telegram.open()
            assert self._telegram._session is not None
            url = f"{self._telegram._file_base_url}/{remote_path}"
            async with self._telegram._session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.read()
            dest.write_bytes(data)

            size_str = _format_file_size(len(data))
            await self._telegram.send_message(
                chat_id, f"\u2705 已保存: {dest.name} ({size_str})\n路径: {dest}"
            )
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"保存文件失败: {exc}")

    # ─── iTerm2 控制命令 ───

    async def _send_tabs(self, chat_id: int) -> None:
        """发送 iTerm2 tab 列表。"""
        try:
            text = await self._iterm.list_tabs_text()
            await self._telegram.send_message(chat_id, text)
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"获取 tab 失败: {exc}")

    async def _use_tab(self, chat_id: int, tab_number: str) -> None:
        """按编号切换默认 tab。"""
        try:
            selected = await self._iterm.set_default_tab(tab_number.strip())
            last_lines = await self._iterm.read_last_lines(10)
            msg = f"切换成功: tab 编号 {selected}\n\n{last_lines or '(空)'}"
            await self._telegram.send_message(chat_id, msg)
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"切换失败: {exc}")

    async def _new_tab(self, chat_id: int) -> None:
        """新建 iTerm2 tab。"""
        try:
            tab_number = await self._iterm.create_new_tab()
            await self._telegram.send_message(
                chat_id,
                f"已新建并切换到 tab 编号: {tab_number}",
            )
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"新建 tab 失败: {exc}")

    async def _send_raw_text(self, chat_id: int, raw_text: str) -> None:
        """只输入文本，不追加回车。"""
        try:
            await self._iterm.send_text(raw_text, enter=False)
            await self._telegram.send_message(chat_id, "已输入")
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"输入失败: {exc}")

    async def _ctrl_c(self, chat_id: int) -> None:
        """发送 Ctrl+C。"""
        try:
            await self._iterm.send_ctrl_c()
            await self._telegram.send_message(chat_id, "已发送 Ctrl+C")
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"发送 Ctrl+C 失败: {exc}")

    async def _enter(self, chat_id: int) -> None:
        """只发送回车键。"""
        try:
            await self._iterm.send_enter()
            await self._telegram.send_message(chat_id, "已发送回车")
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"发送回车失败: {exc}")

    async def _ctrl_d(self, chat_id: int) -> None:
        """发送 Ctrl+D。"""
        try:
            await self._iterm.send_ctrl_d()
            await self._telegram.send_message(chat_id, "已发送 Ctrl+D")
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"发送 Ctrl+D 失败: {exc}")

    async def _last_lines(self, chat_id: int, raw_count: str) -> None:
        """解析倒数行数并发送终端文本。"""
        try:
            count = max(1, min(int(raw_count.strip()), 200))
        except ValueError:
            await self._telegram.send_message(chat_id, "用法: /last <n>")
            return
        await self._send_last_lines(chat_id, count)

    async def _send_last_lines(self, chat_id: int, count: int) -> None:
        """发送当前终端倒数 N 行，优先以 Markdown entities 渲染。"""
        try:
            text = await self._iterm.read_last_lines(count)
            if not text:
                await self._telegram.send_message(chat_id, "(空)")
                return
            body = _clean_tui_output(text).strip() or text.strip()
            await self._telegram.send_markdown_message(chat_id, f"```\n{body}\n```")
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"读取终端失败: {exc}")

    # ─── 文件发送 ───

    async def _handle_fetch_file_or_dir(self, chat_id: int) -> None:
        """弹出选择方式：输入路径 或 目录浏览。"""
        markup = {
            "inline_keyboard": [
                [
                    {"text": "\u2328 输入路径", "callback_data": "fb:input_mode"},
                    {"text": "\U0001f4c2 目录浏览", "callback_data": "fb:browse:/"},
                ]
            ]
        }
        await self._telegram.send_message_with_reply_markup(
            chat_id, "选择获取方式：", markup
        )

    async def _handle_filebrowser_callback(
        self,
        callback_id: str,
        data: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        """处理文件浏览器 Inline Keyboard 的按钮点击。"""
        if data == "fb:input_mode":
            self._awaiting_path_input = True
            try:
                await self._telegram._request("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "请输入文件或目录的完整路径：",
                    "reply_markup": {"inline_keyboard": []},
                })
            except RuntimeError:
                pass
            await self._telegram.answer_callback_query(callback_id)
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            await self._telegram.answer_callback_query(callback_id, "无效操作")
            return
        action, path_str = parts[1], parts[2]

        if action == "browse":
            text, markup = _build_filebrowser_keyboard(path_str)
            try:
                await self._telegram._request("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": markup,
                })
            except RuntimeError:
                pass
            await self._telegram.answer_callback_query(callback_id)

        elif action == "pick":
            p = Path(path_str)
            if not p.exists():
                await self._telegram.answer_callback_query(callback_id, "文件不存在")
                return
            size_str = _format_file_size(p.stat().st_size)
            icon = "\U0001f5bc" if _is_image_file(p) else "\U0001f4c4"
            confirm_text = f"{icon} 已选中: {p.name}\n大小: {size_str}\n路径: {p}"
            confirm_cb = f"fb:confirm:{p}"
            back_cb = f"fb:browse:{p.parent}"
            rows = []
            if len(confirm_cb) <= 64:
                row = [{"text": "\u2705 确认发送", "callback_data": confirm_cb}]
                if len(back_cb) <= 64:
                    row.append({"text": "\u2b05 返回", "callback_data": back_cb})
                rows.append(row)
            try:
                await self._telegram._request("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": confirm_text,
                    "reply_markup": {"inline_keyboard": rows},
                })
            except RuntimeError:
                pass
            await self._telegram.answer_callback_query(callback_id)

        elif action == "confirm":
            await self._telegram.answer_callback_query(callback_id, "发送中...")
            try:
                await self._telegram._request("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": f"正在发送 {Path(path_str).name}...",
                    "reply_markup": {"inline_keyboard": []},
                })
            except RuntimeError:
                pass
            await self._send_local_path(chat_id, Path(path_str))

        elif action == "send_dir":
            await self._telegram.answer_callback_query(callback_id, "打包中...")
            p = Path(path_str)
            if not p.is_dir():
                await self._telegram.send_message(chat_id, f"目录不存在: {path_str}")
                return
            try:
                archive = _tar_gz_directory(p, _FB_SEND_DIR)
                await self._send_local_path(chat_id, archive)
            finally:
                archive_path = _FB_SEND_DIR / f"{p.name}.tar.gz"
                archive_path.unlink(missing_ok=True)

        else:
            await self._telegram.answer_callback_query(callback_id, "未知操作")

    async def _send_local_path(self, chat_id: int, path: Path) -> None:
        """发送本地文件到 Telegram：图片用 sendPhoto，其他用 sendDocument，大文件自动分片。"""
        if not path.exists():
            await self._telegram.send_message(chat_id, f"文件不存在: {path}")
            return

        file_size = path.stat().st_size

        if _is_image_file(path) and file_size <= 10 * 1024 * 1024:
            try:
                await self._telegram.send_photo(chat_id, str(path), caption=path.name)
                return
            except RuntimeError:
                pass

        if file_size <= TELEGRAM_FILE_LIMIT:
            await self._telegram.send_document(chat_id, str(path), caption=path.name)
            return

        await self._telegram.send_message(
            chat_id, f"文件 {path.name} ({file_size // 1024 // 1024}MB) 超限，自动分片发送..."
        )
        parts = _split_file(path)
        try:
            for i, part in enumerate(parts, 1):
                await self._telegram.send_document(
                    chat_id,
                    str(part),
                    caption=f"{path.name} ({i}/{len(parts)})",
                )
        finally:
            for part in parts:
                part.unlink(missing_ok=True)


# ─── 辅助函数 ───


TUI_DECORATION_CHARS = set("─━═▄▀▌▐█░▒▓╔╗╚╝╠╣╦╩╬│┃┌┐└┘├┤┬┴┼")
TABLE_BORDER_CHARS = set("┌┐└┘├┤┬┴┼│─╔╗╚╝╠╣╦╩╬║═")


def _is_decoration_line(line: str) -> bool:
    """判断一行是否全由 TUI 装饰字符组成（分隔线、边框等）。"""
    stripped = line.strip()
    if not stripped:
        return False
    return all(ch in TUI_DECORATION_CHARS or ch == " " for ch in stripped)


def _is_table_line(line: str) -> bool:
    """判断一行是否是 TUI 表格行（包含 │ 分隔的单元格）。"""
    stripped = line.strip()
    return "│" in stripped or "║" in stripped


def _is_table_border(line: str) -> bool:
    """判断一行是否是表格边框行（┌─┬─┐、├─┼─┤、└─┴─┘ 等）。"""
    stripped = line.strip()
    if not stripped:
        return False
    return all(ch in TABLE_BORDER_CHARS or ch == " " for ch in stripped)


def _convert_table_to_list(lines: list[str]) -> list[str]:
    """将 TUI 表格行转换为列表格式。

    输入：│ Skill │ 路径 │ 简述 │
    输出：Skill: 路径 / 简述
    """
    result: list[str] = []
    header_cells: list[str] = []

    for line in lines:
        if _is_table_border(line):
            continue
        if not _is_table_line(line):
            result.append(line)
            continue

        cells = [c.strip() for c in line.split("│") if c.strip()]
        if not cells:
            cells = [c.strip() for c in line.split("║") if c.strip()]
        if not cells:
            continue

        if not header_cells:
            header_cells = cells
            continue

        if len(cells) == len(header_cells):
            parts = []
            for h, v in zip(header_cells, cells):
                if v:
                    parts.append(f"{h}: {v}")
            result.append("  ".join(parts))
        else:
            result.append(" | ".join(cells))

    return result


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clean_tui_output(text: str) -> str:
    """清理 TUI 输出：移除填充装饰，把框线表格转成简洁列表。"""
    lines = text.splitlines()
    cleaned = [line for line in lines if not _is_fill_decoration(line)]

    # 检测连续的 TUI 表格区段并转换为列表格式
    result: list[str] = []
    table_buf: list[str] = []
    for line in cleaned:
        if _is_table_border(line) or _is_table_line(line):
            table_buf.append(line)
        else:
            if table_buf:
                result.extend(_convert_table_to_list(table_buf))
                table_buf = []
            result.append(line)
    if table_buf:
        result.extend(_convert_table_to_list(table_buf))

    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


def _is_fill_decoration(line: str) -> bool:
    """判断一行是否是纯填充装饰（▄▀█░▒▓ 等），但保留表格边框。"""
    stripped = line.strip()
    if not stripped:
        return False
    fill_chars = set("▄▀▌▐█░▒▓")
    return all(ch in fill_chars or ch == " " for ch in stripped)


_IMAGE_PATH_RE = re.compile(
    r"(/[\w./\-]+\.(?:png|jpg|jpeg|gif|webp|bmp))",
    re.IGNORECASE,
)


def _extract_image_paths(text: str) -> list[str]:
    """从文本中提取存在的本机图片文件路径（去重、保序）。"""
    paths = _IMAGE_PATH_RE.findall(text)
    return [p for p in dict.fromkeys(paths) if os.path.isfile(p)]


TELEGRAM_FILE_LIMIT = 49 * 1024 * 1024  # 49MB，留 1MB 余量
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_FB_SEND_DIR = Path(tempfile.gettempdir()) / "tg2iterm2_send"
_FB_MAX_ENTRIES = 30
_FB_MAX_BUTTONS_PER_ROW = 2


def _is_image_file(path: Path) -> bool:
    """按扩展名判断是否图片。"""
    return path.suffix.lower() in _IMAGE_EXTENSIONS


def _format_file_size(size: int) -> str:
    """将字节数格式化为可读字符串。"""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    return f"{size / 1024 / 1024 / 1024:.1f}GB"


def _tar_gz_directory(dir_path: Path, output_dir: Path) -> Path:
    """将文件夹打包为 tar.gz，返回压缩文件路径（跟随符号链接，打包实际文件）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{dir_path.name}.tar.gz"
    archive_path = output_dir / archive_name
    archive_path.unlink(missing_ok=True)

    def _add_item(tar: tarfile.TarFile, full_path: Path, arcname: str) -> None:
        """递归添加文件/目录，遇到符号链接则跟随指向的实际目标。"""
        # 如果是符号链接，获取其指向的实际路径
        if full_path.is_symlink():
            try:
                resolved = full_path.resolve(strict=True)
                if resolved.exists():
                    full_path = resolved
            except (OSError, ValueError):
                # 链接失效或无法解析，跳过
                return

        if full_path.is_dir():
            # 添加目录条目
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
            # 递归处理目录内容
            try:
                for child in sorted(full_path.iterdir(), key=lambda x: x.name):
                    if child.name.startswith("."):
                        continue
                    _add_item(tar, child, f"{arcname}/{child.name}")
            except PermissionError:
                pass
        elif full_path.is_file():
            # 添加普通文件
            tar.add(str(full_path), arcname=arcname)

    with tarfile.open(archive_path, "w:gz") as tar:
        _add_item(tar, dir_path.resolve(), dir_path.name)
    return archive_path


def _split_file(file_path: Path, chunk_size: int = TELEGRAM_FILE_LIMIT) -> list[Path]:
    """将大文件切成多个分片，返回分片路径列表。"""
    _FB_SEND_DIR.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    idx = 1
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            part_path = _FB_SEND_DIR / f"{file_path.name}.part{idx:02d}"
            part_path.write_bytes(chunk)
            parts.append(part_path)
            idx += 1
    return parts


def _build_filebrowser_keyboard(dir_path: str) -> tuple[str, dict[str, Any]]:
    """构建目录浏览的 Inline Keyboard 和消息文本。"""
    p = Path(dir_path)
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return f"无权限访问 {dir_path}", {"inline_keyboard": []}

    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    truncated = False
    all_items = dirs + files
    if len(all_items) > _FB_MAX_ENTRIES:
        all_items = all_items[:_FB_MAX_ENTRIES]
        truncated = True

    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for item in all_items:
        if item.is_dir():
            label = f"\U0001f4c1 {item.name}/"
            cb = f"fb:browse:{item}"
        else:
            prefix = "\U0001f5bc" if _is_image_file(item) else "\U0001f4c4"
            label = f"{prefix} {item.name}"
            cb = f"fb:pick:{item}"
        if len(cb) > 64:
            continue
        row.append({"text": label, "callback_data": cb})
        if len(row) >= _FB_MAX_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav_row: list[dict[str, str]] = []
    parent = str(p.parent)
    if str(p) != "/":
        parent_cb = f"fb:browse:{parent}"
        if len(parent_cb) <= 64:
            nav_row.append({"text": "\u2b06 \u4e0a\u7ea7", "callback_data": parent_cb})
    send_dir_cb = f"fb:send_dir:{p}"
    if len(send_dir_cb) <= 64:
        nav_row.append({"text": "\u2705 \u53d1\u9001\u5f53\u524d\u76ee\u5f55", "callback_data": send_dir_cb})
    if nav_row:
        rows.append(nav_row)

    text = f"\U0001f4c2 {p}"
    if truncated:
        text += f"\n(\u4ec5\u663e\u793a\u524d {_FB_MAX_ENTRIES} \u9879)"
    return text, {"inline_keyboard": rows}


def render_stream_message(
    command: str,
    output: str,
    finished: bool,
    exit_status: int | None = None,
) -> str:
    """渲染流式命令状态文本。

    CLI 输出先清理 TUI 装饰；清理后为空则回退原始文本。
    """
    status = "✅ 已完成" if finished else "⏳ 执行中"
    exit_text = "" if exit_status is None else f" exit={exit_status}"
    body = _clean_tui_output(output).strip() or output.strip()
    if not body:
        return f"{status}{exit_text}\n(暂无输出)"
    return f"{status}{exit_text}\n{body}"


OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.+)$")

# Cursor CLI 权限选项格式：→ Allow search (y) 或 Skip (esc or n)
CURSOR_OPTION_RE = re.compile(r"^\s*(?:→\s+)?(.+?)\s*\(([^)]+)\)\s*$")
CURSOR_OPTION_MARKER = "→"


def parse_selection_options(screen_text: str) -> list[tuple[str, str]]:
    """从 iTerm2 屏幕文本解析当前活跃的选择选项。

    支持两种格式：
    - Claude 格式：❯ 1. Allow  /  2. Deny
    - Cursor 格式：→ Allow search (y)  /  Skip (esc or n)
    """
    claude_options = _parse_claude_options(screen_text)
    if claude_options:
        return claude_options
    return _parse_cursor_options(screen_text)


def _parse_claude_options(screen_text: str) -> list[tuple[str, str]]:
    """解析 Claude 格式的编号选项（❯ 1. xxx）。"""
    lines = screen_text.splitlines()

    cursor_line_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        if "❯" in cleaned and OPTION_RE.match(cleaned):
            cursor_line_idx = idx
            break

    if cursor_line_idx < 0:
        return []

    start = cursor_line_idx
    for idx in range(cursor_line_idx - 1, -1, -1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        if OPTION_RE.match(cleaned):
            start = idx
        else:
            break

    end = cursor_line_idx
    for idx in range(cursor_line_idx + 1, len(lines)):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        if OPTION_RE.match(cleaned):
            end = idx
        else:
            break

    options: list[tuple[str, str]] = []
    for idx in range(start, end + 1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        match = OPTION_RE.match(cleaned)
        if match:
            options.append((match.group(1), match.group(2).strip()))

    return options


def _parse_cursor_options(screen_text: str) -> list[tuple[str, str]]:
    """解析 Cursor 格式的选项（→ Allow search (y) / Skip (esc or n)）。

    Cursor 的权限弹窗格式：
      → Allow search (y)
        Auto-run everything (shift+tab)
        Skip (esc or n)

    → 标记当前选中项，其他选项无标记但有 (快捷键) 后缀。
    """
    lines = screen_text.splitlines()

    # 找到包含 → 标记的行
    marker_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ").strip()
        if cleaned.startswith("→") and CURSOR_OPTION_RE.match(cleaned):
            marker_idx = idx
            break

    if marker_idx < 0:
        return []

    # 从 → 所在行向下收集连续的选项行
    options: list[tuple[str, str]] = []
    for idx in range(marker_idx, len(lines)):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ").strip()
        if not cleaned:
            continue
        match = CURSOR_OPTION_RE.match(cleaned)
        if match:
            label = match.group(1).strip().lstrip("→").strip()
            shortcut = match.group(2).strip()
            options.append((str(len(options) + 1), f"{label} ({shortcut})"))
        elif options:
            break

    return options


def extract_question_text(screen_text: str) -> str:
    """从屏幕文本中提取选项块上方的问题行。"""
    lines = screen_text.splitlines()

    cursor_line_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        if "❯" in cleaned and OPTION_RE.match(cleaned):
            cursor_line_idx = idx
            break

    if cursor_line_idx < 0:
        return ""

    first_option_idx = cursor_line_idx
    for idx in range(cursor_line_idx - 1, -1, -1):
        cleaned = lines[idx].replace("\x00", "").replace("\xa0", " ")
        if OPTION_RE.match(cleaned):
            first_option_idx = idx
        else:
            break

    for idx in range(first_option_idx - 1, -1, -1):
        stripped = lines[idx].replace("\x00", "").replace("\xa0", " ").strip()
        if stripped:
            return stripped
    return ""


def _slash_to_tg_command(name: str) -> str:
    """将 skill 名转为 Telegram 合法命令名。"""
    cmd = name.replace("-", "_").replace(":", "_").replace(".", "_")
    cmd = re.sub(r"[^a-z0-9_]", "", cmd.lower())
    if len(cmd) > 32:
        cmd = cmd[:32]
    return cmd.rstrip("_") or "cmd"


def _set_active_marker(mode: SessionMode, active: bool) -> None:
    """创建或删除 CLI 模式标记文件。

    创建时写入 JSON（不含 conversation_id，等 hook 首次调用时自动绑定）。
    """
    if mode == SessionMode.CURSOR:
        marker = CURSOR_ACTIVE_MARKER
    elif mode == SessionMode.CLAUDE:
        marker = CLAUDE_ACTIVE_MARKER
    else:
        return
    if active:
        marker.write_text(json.dumps({"activated_at": int(__import__("time").time())}))
    else:
        marker.unlink(missing_ok=True)


def _get_session_file(mode: SessionMode) -> Path | None:
    """返回对应模式的 session 持久化文件路径。"""
    if mode == SessionMode.CURSOR:
        return CURSOR_SESSION_FILE
    if mode == SessionMode.CLAUDE:
        return CLAUDE_SESSION_FILE
    return None


def _read_session_id(mode: SessionMode) -> str | None:
    """从持久化文件读取上次的 session/conversation ID。

    同时检查标记文件中是否有 hook 绑定的 ID（优先使用）。
    """
    if mode == SessionMode.CURSOR:
        marker = CURSOR_ACTIVE_MARKER
    elif mode == SessionMode.CLAUDE:
        marker = CLAUDE_ACTIVE_MARKER
    else:
        return None

    # 先检查标记文件中 hook 绑定的 ID
    try:
        data = json.loads(marker.read_text())
        bound_id = data.get("conversation_id") or data.get("session_id")
        if bound_id:
            _save_session_id(mode, bound_id)
            return bound_id
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    # 再读持久化文件
    session_file = _get_session_file(mode)
    if session_file is None:
        return None
    try:
        data = json.loads(session_file.read_text())
        return data.get("session_id")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _save_session_id(mode: SessionMode, session_id: str) -> None:
    """将 session ID 持久化到文件。"""
    session_file = _get_session_file(mode)
    if session_file is None:
        return
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(json.dumps({"session_id": session_id}))
    except OSError:
        pass


def _clear_session_id(mode: SessionMode) -> None:
    """清除持久化的 session ID。"""
    session_file = _get_session_file(mode)
    if session_file is not None:
        session_file.unlink(missing_ok=True)


def _sync_session_id_from_marker(mode: SessionMode) -> None:
    """从标记文件读取 hook 绑定的 conversation_id，持久化到 session 文件。"""
    if mode == SessionMode.CURSOR:
        marker = CURSOR_ACTIVE_MARKER
        key = "conversation_id"
    elif mode == SessionMode.CLAUDE:
        marker = CLAUDE_ACTIVE_MARKER
        key = "session_id"
    else:
        return
    try:
        data = json.loads(marker.read_text())
        bound_id = data.get(key)
        if bound_id:
            _save_session_id(mode, bound_id)
    except (OSError, json.JSONDecodeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# 提醒模式相关方法（在 Tg2ITermApp 类中）
# ---------------------------------------------------------------------------
