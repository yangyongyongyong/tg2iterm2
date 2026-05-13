#!/Users/luca/miniforge3/envs/py311/bin/python
"""tg2iterm2 本地测试入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import inspect
import os
import shutil
import sys
import tempfile
import bot_app as bot_module
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from adapters.opencode_adapter import OpenCodeAdapter
from bot_app import SessionMode, Tg2ITermApp, _is_invalid_claude_resume_error, _sort_commands_by_usage, render_stream_message
from config import AppConfig, load_config
from iterm_controller import (
    ITermController,
    _clean_generic_delta,
    _clean_opencode_delta,
    _has_cursor_answer,
    _has_cursor_ready_state,
    _has_opencode_answer,
    CLAUDE_DONE_SIGNAL_DEFAULT,
    SCREEN_TEXT_PROMPT_ID,
    clean_claude_delta,
    command_name,
    is_claude_prompt_cursor,
    is_claude_turn_complete,
    output_after,
    parse_tab_number,
)
from telegram_client import TelegramBotClient, limit_telegram_text, sanitize_filename


TEST_CHAT_ID = 1151534243


@dataclass(frozen=True)
class TestCase:
    """保存单个测试用例的元信息。"""

    name: str
    scenario: str
    func: Callable[[], Any]


class FakeTelegram:
    """记录测试中的 Telegram 发送内容。"""

    def __init__(self) -> None:
        """初始化消息列表。"""
        self.messages: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.menu_commands: list[dict[str, str]] | None = None
        self.reply_markups: list[tuple[int, str, dict[str, Any]]] = []

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """记录发送消息并返回假的 Telegram message。"""
        self.messages.append((chat_id, text))
        return {"message_id": len(self.messages)}

    async def send_markdown_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """记录 Markdown 发送，测试中按普通文本处理。"""
        return await self.send_message(chat_id, text)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        """记录编辑消息内容。"""
        self.edits.append((chat_id, message_id, text))

    async def edit_markdown_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        """记录 Markdown 编辑，测试中按普通文本处理。"""
        await self.edit_message_text(chat_id, message_id, text)

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """记录当前注册的 Bot 菜单。"""
        self.menu_commands = commands

    async def send_message_with_reply_markup(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        """记录带按钮的消息发送。"""
        self.reply_markups.append((chat_id, text, reply_markup))
        self.messages.append((chat_id, text))
        return {"message_id": len(self.messages)}

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """测试中忽略 reply_markup 编辑。"""
        _ = chat_id
        _ = message_id
        _ = reply_markup

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """测试中忽略 callback 应答。"""
        _ = callback_query_id
        _ = text


class FakeSession:
    """记录发送到终端 session 的文本。"""

    def __init__(self) -> None:
        self.sent_texts: list[str] = []

    async def async_send_text(self, text: str, suppress_broadcast: bool = False) -> None:
        """记录发送到 session 的文本。"""
        _ = suppress_broadcast
        self.sent_texts.append(text)


class FakeITerm:
    """记录测试中的 iTerm2 调用。"""

    def __init__(self) -> None:
        """初始化命令列表。"""
        self.commands: list[str] = []
        self.enter_count = 0
        self.created_tab_count = 0
        self.created_tab_activate_flags: list[bool] = []
        self.target_session = FakeSession()
        self.foreground_state: dict[str, Any] | None = None
        self.command_results: list[Any] = []
        self.closed_target_tab_count = 0
        self.default_tab_cleared = False
        self.simulate_stale_foreground_empty_output = False
        self.wait_shell_ready_count = 0

    async def run_command_stream(
        self,
        command: str,
        on_update: Callable[[str], Any],
        stream_interval: float,
    ) -> Any:
        """记录普通命令输入。"""
        self.commands.append(command)
        await maybe_await(on_update("fake output"))
        if self.simulate_stale_foreground_empty_output and self.foreground_state is not None:
            return type("Result", (), {"exit_status": 0, "output": ""})()
        if self.command_results:
            return self.command_results.pop(0)
        return type("Result", (), {"exit_status": 0, "output": "fake output"})()

    async def send_enter(self) -> None:
        """记录发送回车。"""
        self.enter_count += 1

    async def create_new_tab(self, activate: bool = True) -> int:
        """记录新建 tab，并返回新的用户可见编号。"""
        self.created_tab_count += 1
        self.created_tab_activate_flags.append(activate)
        return self.created_tab_count

    async def get_target_session(self) -> FakeSession:
        """返回假的当前目标 session。"""
        return self.target_session

    async def wait_until_shell_ready(self, timeout: float = 30.0) -> None:
        """记录等待 shell prompt ready。"""
        _ = timeout
        self.wait_shell_ready_count += 1

    def _set_foreground_state(
        self,
        session: FakeSession,
        prompt_id: str | None,
        command_name: str | None,
    ) -> None:
        """记录当前前台命令状态。"""
        self.foreground_state = {
            "session": session,
            "prompt_id": prompt_id,
            "command_name": command_name,
        }

    async def close_target_tab(self) -> None:
        """记录关闭当前目标 tab，并清理前台状态。"""
        self.closed_target_tab_count += 1
        self.foreground_state = None
        self.default_tab_cleared = True

    def clear_default_tab(self) -> None:
        """记录清除默认 tab 绑定。"""
        self.default_tab_cleared = True


class SequenceOutputController(ITermController):
    """用预设序列模拟 iTerm2 prompt output_range 的读取过程。"""

    def __init__(self, outputs: list[str]) -> None:
        """保存每次读取应返回的输出。"""
        self._outputs = outputs
        self._index = 0

    async def _read_current_command_output(
        self,
        _session: Any,
        _prompt_id: str | None,
    ) -> str:
        """按顺序返回输出；序列耗尽后保持最后一次输出。"""
        if not self._outputs:
            return ""
        if self._index >= len(self._outputs):
            return self._outputs[-1]
        output = self._outputs[self._index]
        self._index += 1
        return output


class SubmitProbeController(ITermController):
    """只用于验证 send_foreground_input_stream 的发送序列。"""

    def __init__(self, command_name: str) -> None:
        super().__init__()
        self.probe_session = FakeSession()
        self._foreground_command_name = command_name

    async def _wait_foreground_prompt_id(self, timeout: float = 2.0) -> tuple[Any, str | None]:
        _ = timeout
        return self.probe_session, "prompt-1"

    async def _read_current_command_output_after_settle(
        self,
        session: Any,
        prompt_id: str | None,
    ) -> str:
        _ = session
        _ = prompt_id
        return ""

    async def _collect_foreground_delta(
        self,
        session: Any,
        prompt_id: str,
        before_output: str,
        submitted_text: str,
        is_interactive_cli: bool,
        on_update: Any,
        stream_interval: float,
        idle_seconds: float,
        before_ns: int = 0,
    ) -> str:
        _ = session
        _ = prompt_id
        _ = before_output
        _ = submitted_text
        _ = is_interactive_cli
        _ = on_update
        _ = stream_interval
        _ = idle_seconds
        _ = before_ns
        return ""


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    """断言两个值相等。"""
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r}, actual={actual!r}")


def assert_true(value: bool, message: str) -> None:
    """断言值为真。"""
    if not value:
        raise AssertionError(message)


def assert_contains(text: str, needle: str, message: str) -> None:
    """断言文本包含指定片段。"""
    if needle not in text:
        raise AssertionError(f"{message}: needle={needle!r}, text={text!r}")


def assert_not_contains(text: str, needle: str, message: str) -> None:
    """断言文本不包含指定片段。"""
    if needle in text:
        raise AssertionError(f"{message}: needle={needle!r}, text={text!r}")


async def maybe_await(value: Any) -> Any:
    """兼容同步和异步回调。"""
    if inspect.isawaitable(value):
        return await value
    return value


def build_unit_cases() -> list[TestCase]:
    """构造默认本地回归测试用例。"""
    return [
        TestCase(
            name="config_env",
            scenario="配置读取: 从环境变量读取 bot token、授权 chat id 和流式间隔",
            func=test_config_env,
        ),
        TestCase(
            name="telegram_text_limit",
            scenario="Telegram 文本限制: 超长输出保留尾部并加截断标记",
            func=test_telegram_text_limit,
        ),
        TestCase(
            name="sanitize_filename",
            scenario="图片文件名: Telegram 文件名前缀只保留安全字符",
            func=test_sanitize_filename,
        ),
        TestCase(
            name="image_path_prefix",
            scenario="图片下一句话: 暂存图片路径会前置到下一条终端输入",
            func=test_image_path_prefix,
        ),
        TestCase(
            name="unauthorized_chat",
            scenario="鉴权: 非授权私聊不能触发 iTerm2 命令",
            func=test_unauthorized_chat,
        ),
        TestCase(
            name="unknown_slash_passthrough",
            scenario="消息路由: 未知斜杠文本按普通终端命令透传",
            func=test_unknown_slash_passthrough,
        ),
        TestCase(
            name="shell_command_background_tab",
            scenario="Shell 命令: 默认在后台新建并绑定独立 iTerm2 tab 执行",
            func=test_shell_command_uses_background_tab,
        ),
        TestCase(
            name="enter_command",
            scenario="控制命令: /enter 只发送回车键，不走普通命令执行",
            func=test_enter_command,
        ),
        TestCase(
            name="usage_refreshes_menu",
            scenario="命令统计: 每次使用命令后都会异步刷新 Bot 菜单排序",
            func=test_usage_refreshes_menu,
        ),
        TestCase(
            name="render_stream_message",
            scenario="机器人响应: 命令执行中和已完成的消息格式稳定",
            func=test_render_stream_message,
        ),
        TestCase(
            name="tab_number_parse",
            scenario="tab 编号: 只接受大于 0 的用户可见编号",
            func=test_tab_number_parse,
        ),
        TestCase(
            name="command_name_parse",
            scenario="交互式命令识别: 支持环境变量前缀后的 claude/python 命令名",
            func=test_command_name_parse,
        ),
        TestCase(
            name="claude_delta_anchor",
            scenario="Claude 输出切分: 第二轮交互不带上第一轮历史内容",
            func=test_claude_delta_anchor,
        ),
        TestCase(
            name="opencode_launch_command",
            scenario="OpenCode 启动命令: 新会话和旧会话都必须固定带上上下文目录参数",
            func=test_opencode_launch_command,
        ),
        TestCase(
            name="command_usage_sort",
            scenario="菜单排序: 高频命令排在前面且同频保持原顺序",
            func=test_command_usage_sort,
        ),
        TestCase(
            name="telegram_command_passthrough",
            scenario="Telegram 菜单注册: 传入排序后的命令列表时不能再额外前置固定命令",
            func=test_telegram_command_passthrough,
        ),
        TestCase(
            name="opencode_mode_menu",
            scenario="OpenCode 交互模式: Bot 菜单应切换为精简专用命令集",
            func=test_opencode_mode_menu,
        ),
        TestCase(
            name="opencode_project_candidates",
            scenario="OpenCode 项目列表: 合并数据库与手动项目，并按 bot 使用频率降序排序",
            func=test_opencode_project_candidates,
        ),
        TestCase(
            name="opencode_project_select_flow",
            scenario="OpenCode 项目选择: 发送序号后应进入对应项目目录",
            func=test_opencode_project_select_flow,
        ),
        TestCase(
            name="opencode_project_add_flow",
            scenario="OpenCode 项目添加: 手动添加目录后应持久化并直接进入项目",
            func=test_opencode_project_add_flow,
        ),
        TestCase(
            name="opencode_project_picker_layout",
            scenario="OpenCode 项目列表: 支持分页，并按置顶/收藏/最近分组展示",
            func=test_opencode_project_picker_layout,
        ),
        TestCase(
            name="exit_closes_cli_tab",
            scenario="CLI 退出: /exit 后应自动关闭当前绑定 tab 并清理目标绑定",
            func=test_exit_closes_cli_tab,
        ),
        TestCase(
            name="opencode_exit_then_shell_output",
            scenario="OpenCode 退出后: 执行 shell 命令不应因为残留前台状态而变成空输出",
            func=test_opencode_exit_then_shell_output,
        ),
        TestCase(
            name="claude_mode_new_tab",
            scenario="Claude 交互模式: 进入时应先新建并绑定独立 iTerm2 tab",
            func=test_claude_mode_creates_new_tab,
        ),
        TestCase(
            name="opencode_mode_new_tab",
            scenario="OpenCode 交互模式: 进入时应先新建并绑定独立 iTerm2 tab",
            func=test_opencode_mode_creates_new_tab,
        ),
        TestCase(
            name="cursor_mode_screen_binding",
            scenario="Cursor 交互模式: 进入时应直接绑定整屏前台会话，避免 prompt 状态丢失",
            func=test_cursor_mode_uses_screen_binding,
        ),
        TestCase(
            name="cursor_double_enter_submit",
            scenario="Cursor 交互输入: 为稳定提交应发送双回车",
            func=test_cursor_double_enter_submit,
        ),
        TestCase(
            name="claude_invalid_resume_error",
            scenario="Claude 会话恢复: 失效 resume 错误应被识别",
            func=test_claude_invalid_resume_error,
        ),
        TestCase(
            name="claude_silent_invalid_resume_fallback",
            scenario="Claude 静默执行: 遇到失效 session_id 时应自动清理并重试新会话",
            func=test_claude_silent_invalid_resume_fallback,
        ),
        TestCase(
            name="claude_interactive_invalid_resume_fallback",
            scenario="Claude 交互模式: 遇到失效 session_id 时应自动切换到新会话",
            func=test_claude_interactive_invalid_resume_fallback,
        ),
        TestCase(
            name="opencode_input_anchor",
            scenario="OpenCode 输出切分: `┃` 输入回显行也能定位到本轮回答",
            func=test_opencode_input_anchor,
        ),
        TestCase(
            name="opencode_delta_clean",
            scenario="OpenCode 文本清理: 输入栏、思考侧栏和底部状态栏不能进入最终回答",
            func=test_opencode_delta_clean,
        ),
        TestCase(
            name="claude_repeated_input_anchor",
            scenario="Claude 输出切分: 答案里重复用户输入时不能从答案中间截断",
            func=test_claude_repeated_input_anchor,
        ),
        TestCase(
            name="claude_empty_before_no_history",
            scenario="Claude 输出切分: before_output 偶发为空时不能返回上一轮历史",
            func=test_claude_empty_before_no_history,
        ),
        TestCase(
            name="cursor_pasted_input_anchor",
            scenario="Cursor 输出切分: iTerm2 粘贴回显前缀不能导致本轮回答丢失",
            func=test_cursor_pasted_input_anchor,
        ),
        TestCase(
            name="cursor_follow_up_complete",
            scenario="Cursor 完成判断: Add a follow-up 和 system-reminder 尾屏仍应视为已完成",
            func=test_cursor_follow_up_complete,
        ),
        TestCase(
            name="cursor_system_reminder_not_answer",
            scenario="Cursor 回答判断: 只有 system-reminder 时不能提前当成已拿到回答",
            func=test_cursor_system_reminder_not_answer,
        ),
        TestCase(
            name="cursor_composer_status_clean",
            scenario="Cursor 文本清理: 无 Auto-run 的 Composer 状态栏也不能进入最终回答",
            func=test_cursor_composer_status_clean,
        ),
        TestCase(
            name="prompt_output_settle",
            scenario="iTerm2 输出读取: output_range 先半截后完整时必须等稳定",
            func=test_prompt_output_settle,
        ),
        TestCase(
            name="claude_long_answer_preserve",
            scenario="Claude 文本清理: 多行 skill 列表不能只保留前几行",
            func=test_claude_long_answer_preserve,
        ),
        TestCase(
            name="claude_nul_space_preserve",
            scenario="Claude 文本清理: iTerm2 空白占位符不能把英文单词粘连",
            func=test_claude_nul_space_preserve,
        ),
        TestCase(
            name="claude_thinking_not_complete",
            scenario="Claude 完成判断: 底部仍是思考状态行时不能返回已完成",
            func=test_claude_thinking_not_complete,
        ),
        TestCase(
            name="claude_tool_running_not_complete",
            scenario="Claude 完成判断: 回答后仍有工具 Running 时不能返回已完成",
            func=test_claude_tool_running_not_complete,
        ),
        TestCase(
            name="claude_spinner_tip_not_complete",
            scenario="Claude 完成判断: 只有 Orchestrating 和 tip 时不能返回已完成",
            func=test_claude_spinner_tip_not_complete,
        ),
        TestCase(
            name="claude_new_spinner_tip_not_complete",
            scenario="Claude 完成判断: 新版 ✽ spinner 和 ⎿ Tip 不能返回已完成",
            func=test_claude_new_spinner_tip_not_complete,
        ),
        TestCase(
            name="claude_prompt_complete",
            scenario="Claude 完成判断: 屏幕底部回到空 prompt 且光标在右侧才完成",
            func=test_claude_prompt_complete,
        ),
        TestCase(
            name="claude_cursor_position",
            scenario="Claude 光标判断: 光标必须在 prompt 符号右侧",
            func=test_claude_cursor_position,
        ),
        TestCase(
            name="claude_unknown_progress_clean",
            scenario="Claude 状态词清理: 未知进度词变化也不会污染最终答案",
            func=test_claude_unknown_progress_clean,
        ),
        TestCase(
            name="claude_real_screen_tail",
            scenario="Claude 真实屏幕: prompt 后有分隔线和填充字符时仍能判断完成",
            func=test_claude_real_screen_tail,
        ),
        TestCase(
            name="hook_signal_detection",
            scenario="Hook 信号检测: 信号文件时间戳大于发送时间时返回已完成",
            func=test_hook_signal_detection,
        ),
    ]


def test_config_env() -> None:
    """测试环境变量配置读取。"""
    old_values = {
        "TG_BOT_TOKEN": os.environ.get("TG_BOT_TOKEN"),
        "TG_ALLOWED_CHAT_ID": os.environ.get("TG_ALLOWED_CHAT_ID"),
        "TG_STREAM_INTERVAL": os.environ.get("TG_STREAM_INTERVAL"),
    }
    try:
        os.environ["TG_BOT_TOKEN"] = "dummy-token"
        os.environ["TG_ALLOWED_CHAT_ID"] = str(TEST_CHAT_ID)
        os.environ["TG_STREAM_INTERVAL"] = "0.25"
        config = load_config()
        assert_equal(config.bot_token, "dummy-token", "bot token 应读取环境变量")
        assert_equal(config.allowed_chat_id, TEST_CHAT_ID, "授权 chat id 应读取环境变量")
        assert_equal(config.stream_interval, 0.25, "流式间隔应读取环境变量")
    finally:
        restore_env(old_values)


def test_telegram_text_limit() -> None:
    """测试 Telegram 超长文本截断。"""
    text = "a" * 5000
    limited = limit_telegram_text(text)
    assert_true(len(limited) <= 4000, "Telegram 文本应限制在 4000 字符以内")
    assert_contains(limited, "前面内容已截断", "截断文本应包含明确标记")


def test_sanitize_filename() -> None:
    """测试临时图片文件名清理。"""
    assert_equal(
        sanitize_filename("a/b:c 图片.png"),
        "a_b_c_图片.png",
        "文件名特殊字符应替换为下划线",
    )


def test_image_path_prefix() -> None:
    """测试图片路径前置到下一条文本。"""
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        iterm=FakeITerm(),  # type: ignore[arg-type]
    )
    app._pending_image_paths[TEST_CHAT_ID] = ["/tmp/a.png", "/tmp/b.jpg"]
    assert_equal(
        app._consume_image_paths(TEST_CHAT_ID, "分析这两张图"),
        "/tmp/a.png /tmp/b.jpg 分析这两张图",
        "下一条文本应携带所有暂存图片路径",
    )
    assert_equal(
        app._consume_image_paths(TEST_CHAT_ID, "pwd"),
        "pwd",
        "图片路径消费后不应重复携带",
    )


async def test_unauthorized_chat() -> None:
    """测试非授权 chat id 被拒绝。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._handle_update(
        {
            "message": {
                "chat": {"id": 999, "type": "private"},
                "text": "pwd",
            }
        }
    )
    assert_equal(telegram.messages, [(999, "Forbidden")], "非授权用户应收到 Forbidden")
    assert_equal(iterm.commands, [], "非授权消息不应执行终端命令")


async def test_unknown_slash_passthrough() -> None:
    """测试未知斜杠文本透传到终端。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._dispatch_text(TEST_CHAT_ID, "/unknown_safe_command")
    await asyncio.sleep(0)
    if app._command_task is not None:
        await app._command_task
    assert_equal(
        iterm.commands,
        ["/unknown_safe_command"],
        "未知斜杠文本应作为普通终端命令",
    )
    assert_equal(iterm.created_tab_activate_flags, [False], "普通 shell 命令应在后台新建 tab 执行")


async def test_shell_command_uses_background_tab() -> None:
    """测试普通 shell 文本会在后台新建独立 tab 中执行。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    iterm.command_results = [type("Result", (), {"exit_status": 0, "output": "/Users/luca\n(base) luca@mbp tg2iterm2 %"})()]
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._dispatch_text(TEST_CHAT_ID, "pwd")
    await asyncio.sleep(0)
    if app._command_task is not None:
        await app._command_task
    assert_equal(iterm.created_tab_count, 1, "普通 shell 命令前应先新建一个 tab")
    assert_equal(iterm.created_tab_activate_flags, [False], "普通 shell 命令应在后台新建 tab，不切走当前可见 tab")
    assert_equal(iterm.wait_shell_ready_count, 1, "普通 shell 命令执行前应先等待新 tab 中 shell prompt 就绪")
    assert_equal(iterm.commands, ["pwd"], "普通 shell 文本应在新 tab 中执行")
    assert_contains(telegram.messages[0][1], "当前绑定 tab：1", "执行中提示应回显绑定的 tab 编号")
    assert_contains(telegram.edits[-1][2], "/Users/luca", "shell 最终输出应保留真实命令结果")
    assert_true("(base) luca@mbp tg2iterm2 %" not in telegram.edits[-1][2], "shell 最终输出不应残留尾部 prompt")


async def test_enter_command() -> None:
    """测试 /enter 只发送回车。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._dispatch_text(TEST_CHAT_ID, "/enter")
    assert_equal(iterm.enter_count, 1, "/enter 应只发送一次回车")
    assert_equal(iterm.commands, [], "/enter 不应作为普通命令执行")
    assert_equal(
        telegram.messages[-1],
        (TEST_CHAT_ID, "已发送回车"),
        "/enter 应明确回复发送成功",
    )


async def test_usage_refreshes_menu() -> None:
    """测试记录命令使用后会立即异步刷新菜单。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    app._command_usage = {}
    await app._dispatch_text(TEST_CHAT_ID, "/enter")
    await asyncio.sleep(0)
    if app._menu_update_task is not None:
        await app._menu_update_task
    assert_true(telegram.menu_commands is not None, "记录命令使用后应触发菜单刷新")
    assert_equal(telegram.menu_commands[0]["command"], "enter", "最新高频命令应被刷新到菜单顶部")


def test_render_stream_message() -> None:
    """测试 Telegram 命令响应格式。"""
    running = render_stream_message("pwd", "/tmp/workspace", finished=False)
    done = render_stream_message("echo", "hello-test-output", finished=True, exit_status=0)
    shell_done = render_stream_message(
        "pwd",
        "/Users/luca\n(base) luca@mbp tg2iterm2 %",
        finished=True,
        exit_status=0,
        strip_trailing_shell_prompt=True,
    )
    percent_done = render_stream_message(
        "echo",
        "完成率 95%",
        finished=True,
        exit_status=0,
        strip_trailing_shell_prompt=True,
    )
    assert_contains(running, "执行中", "执行中消息应标明状态")
    assert_contains(running, "/tmp/workspace", "执行中消息应包含输出内容")
    assert_contains(done, "已完成 exit=0", "完成消息应包含退出码")
    assert_contains(done, "hello-test-output", "完成消息应包含输出")
    assert_contains(shell_done, "/Users/luca", "shell 输出应保留命令结果")
    assert_true("(base) luca@mbp tg2iterm2 %" not in shell_done, "shell 输出应去掉尾部 prompt")
    assert_contains(percent_done, "95%", "普通百分号文本不应被误删")


def test_tab_number_parse() -> None:
    """测试 tab 编号解析。"""
    assert_equal(parse_tab_number(" 2 "), 2, "编号字符串应解析为整数")
    for value in ("0", "-1", "abc"):
        with suppress_expected(RuntimeError, f"非法编号 {value} 应报错"):
            parse_tab_number(value)


def test_command_name_parse() -> None:
    """测试命令名解析。"""
    assert_equal(command_name("claude"), "claude", "普通命令名应被识别")
    assert_equal(
        command_name("ANTHROPIC_BASE_URL=http://x claude --debug"),
        "claude",
        "环境变量前缀后的命令名应被识别",
    )
    assert_equal(
        command_name("PYTHONPATH=/tmp /Users/luca/miniforge3/envs/py311/bin/python"),
        "python",
        "绝对路径命令名应取 basename",
    )


def test_claude_delta_anchor() -> None:
    """测试 Claude 本轮输出锚点切分。"""
    before = "❯ old\n\n⏺ old answer\n\n✻ Sautéed for 2s\n\n❯\n"
    current = before + "❯ 5-1\n\nFrosting...\n\n⏺ 4\n\n✻ Cogitated for 11s\n"
    assert_equal(
        output_after(before, current, "5-1"),
        "Frosting...\n\n⏺ 4\n\n✻ Cogitated for 11s\n",
        "本轮输出不应带上上一轮 Claude 回复",
    )


def test_opencode_launch_command() -> None:
    """测试 OpenCode 交互启动命令始终带上下文目录。"""
    adapter = OpenCodeAdapter(Path("/tmp/opencode-context"))
    expected = "opencode -c /tmp/opencode-context"
    assert_equal(adapter.get_launch_command(None), expected, "新会话启动命令应带 -c 上下文目录")
    assert_equal(adapter.get_launch_command("session-123"), expected, "已有会话时也应继续使用固定上下文目录")


def test_command_usage_sort() -> None:
    """测试命令菜单按使用频率降序排序。"""
    commands = [
        {"command": "help", "description": "帮助"},
        {"command": "tabs", "description": "标签"},
        {"command": "opencode", "description": "OpenCode"},
        {"command": "new", "description": "新会话"},
    ]
    usage = {"opencode": 8, "tabs": 3, "new": 3}
    sorted_commands = _sort_commands_by_usage(commands, usage)
    assert_equal(
        [item["command"] for item in sorted_commands],
        ["opencode", "tabs", "new", "help"],
        "高频命令应排前面，同频命令应保持原始顺序",
    )


async def test_telegram_command_passthrough() -> None:
    """测试传入自定义命令列表时 Telegram 客户端不会再追加默认命令。"""

    class RecordingTelegramClient(TelegramBotClient):
        def __init__(self) -> None:
            super().__init__("dummy-token")
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def _request(self, method: str, payload: dict[str, Any]) -> Any:
            self.calls.append((method, payload))
            return True

    client = RecordingTelegramClient()
    custom_commands = [
        {"command": "opencode", "description": "进入 OpenCode 模式"},
        {"command": "tabs", "description": "列出 iTerm2 tab"},
    ]
    await client.set_my_commands(custom_commands)
    method, payload = client.calls[-1]
    assert_equal(method, "setMyCommands", "应调用 setMyCommands API")
    assert_equal(payload["commands"], custom_commands, "自定义菜单顺序不应被默认命令前置打乱")


async def test_opencode_mode_menu() -> None:
    """测试 OpenCode 交互模式会注册精简 Bot 菜单。"""
    telegram = FakeTelegram()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=FakeITerm(),  # type: ignore[arg-type]
    )
    app._session_mode = SessionMode.OPENCODE
    app._command_usage = {}
    await app._update_bot_menu()
    assert_equal(
        telegram.menu_commands,
        [
            {"command": "opencode", "description": "进入 OpenCode 模式"},
            {"command": "opencode_project", "description": "进入指定 OpenCode 项目"},
            {"command": "new", "description": "重置当前 CLI 会话"},
            {"command": "exit", "description": "退出当前 CLI 模式"},
            {"command": "opencode_silent", "description": "OpenCode静默执行"},
            {"command": "tabs", "description": "列出 iTerm2 tab"},
        ],
        "OpenCode 模式下应只显示精简专用菜单",
    )


def test_opencode_project_candidates() -> None:
    """测试 OpenCode 项目列表会按本地使用频率排序。"""
    old_projects_file = bot_module.OPENCODE_PROJECTS_FILE
    old_read_recent = bot_module._read_recent_opencode_project_paths
    with tempfile.TemporaryDirectory(prefix="tg2iterm2_opencode_projects_") as temp_dir:
        projects_file = Path(temp_dir) / "opencode_projects.json"
        try:
            bot_module.OPENCODE_PROJECTS_FILE = projects_file
            projects_file.write_text(json.dumps({
                "manual_paths": ["/repo/manual"],
                "usage": {"/repo/manual": 4, "/repo/db2": 6},
                "aliases": {"/repo/manual": "手动项目"},
            }))
            bot_module._read_recent_opencode_project_paths = lambda limit=20: ["/repo/db1", "/repo/db2"]
            candidates = bot_module._load_opencode_project_candidates(limit=10)
            assert_equal(
                candidates,
                [
                    {"path": "/repo/db2", "alias": "db2", "favorite": "0", "pinned": "0"},
                    {"path": "/repo/manual", "alias": "手动项目", "favorite": "0", "pinned": "0"},
                    {"path": "/repo/db1", "alias": "db1", "favorite": "0", "pinned": "0"},
                ],
                "项目列表应按 bot 侧使用频率降序排序，并保留数据库项目",
            )
        finally:
            bot_module.OPENCODE_PROJECTS_FILE = old_projects_file
            bot_module._read_recent_opencode_project_paths = old_read_recent


async def test_opencode_project_select_flow() -> None:
    """测试 OpenCode 项目选择后会进入对应项目。"""
    old_projects_file = bot_module.OPENCODE_PROJECTS_FILE
    old_read_recent = bot_module._read_recent_opencode_project_paths
    entered: list[tuple[int, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="tg2iterm2_opencode_projects_") as temp_dir:
        project_dir = Path(temp_dir) / "proj-a"
        project_dir.mkdir()
        try:
            bot_module.OPENCODE_PROJECTS_FILE = Path(temp_dir) / "opencode_projects.json"
            bot_module._read_recent_opencode_project_paths = lambda limit=20: [str(project_dir)]
            telegram = FakeTelegram()
            app = Tg2ITermApp(
                config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
                telegram=telegram,  # type: ignore[arg-type]
                iterm=FakeITerm(),  # type: ignore[arg-type]
            )

            async def fake_enter(chat_id: int, initial_prompt: str, context_dir: Path | None = None) -> None:
                entered.append((chat_id, initial_prompt, str(context_dir)))

            app._enter_opencode_interactive_mode = fake_enter  # type: ignore[method-assign]
            await app._dispatch_text(TEST_CHAT_ID, "/opencode_project")
            assert_equal(app._session_mode, SessionMode.OPENCODE_PROJECT_SELECT, "应进入 OpenCode 项目选择模式")
            assert_contains(telegram.messages[-1][1], str(project_dir), "应向用户展示可选项目路径")
            assert_true(bool(telegram.reply_markups), "项目选择应发送带按钮的消息")
            await app._dispatch_text(TEST_CHAT_ID, "1")
            assert_equal(entered, [(TEST_CHAT_ID, "", str(project_dir))], "发送序号后应进入对应项目目录")
        finally:
            bot_module.OPENCODE_PROJECTS_FILE = old_projects_file
            bot_module._read_recent_opencode_project_paths = old_read_recent


async def test_opencode_project_add_flow() -> None:
    """测试手动添加 OpenCode 项目路径后会持久化并直接进入。"""
    old_projects_file = bot_module.OPENCODE_PROJECTS_FILE
    entered: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tg2iterm2_opencode_projects_") as temp_dir:
        project_dir = Path(temp_dir) / "proj-b"
        project_dir.mkdir()
        projects_file = Path(temp_dir) / "opencode_projects.json"
        try:
            bot_module.OPENCODE_PROJECTS_FILE = projects_file
            telegram = FakeTelegram()
            app = Tg2ITermApp(
                config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
                telegram=telegram,  # type: ignore[arg-type]
                iterm=FakeITerm(),  # type: ignore[arg-type]
            )

            async def fake_enter(chat_id: int, initial_prompt: str, context_dir: Path | None = None) -> None:
                _ = chat_id
                _ = initial_prompt
                entered.append(str(context_dir))

            app._enter_opencode_interactive_mode = fake_enter  # type: ignore[method-assign]
            await app._dispatch_text(TEST_CHAT_ID, "/opencode_project_add")
            assert_equal(app._session_mode, SessionMode.OPENCODE_PROJECT_ADD, "应进入 OpenCode 项目添加模式")
            await app._dispatch_text(TEST_CHAT_ID, f"项目B | {project_dir}")
            assert_equal(entered, [str(project_dir)], "手动添加后应直接进入对应项目目录")
            saved = json.loads(projects_file.read_text())
            assert_true(str(project_dir) in saved["manual_paths"], "手动添加的项目路径应被持久化")
            assert_equal(saved["aliases"][str(project_dir)], "项目B", "手动添加的别名应被持久化")
        finally:
            bot_module.OPENCODE_PROJECTS_FILE = old_projects_file


def test_opencode_project_picker_layout() -> None:
    """测试项目选择器支持分组和分页。"""
    projects = [
        {"path": "/repo/pin", "alias": "置顶项目", "favorite": "0", "pinned": "1"},
        {"path": "/repo/fav", "alias": "收藏项目", "favorite": "1", "pinned": "0"},
        {"path": "/repo/recent1", "alias": "最近项目1", "favorite": "0", "pinned": "0"},
        {"path": "/repo/recent2", "alias": "最近项目2", "favorite": "0", "pinned": "0"},
        {"path": "/repo/recent3", "alias": "最近项目3", "favorite": "0", "pinned": "0"},
    ]
    text, markup = bot_module._build_opencode_project_picker(projects, page=0, page_size=3)
    assert_contains(text, "【置顶项目】", "分页列表应显示置顶项目分组")
    assert_contains(text, "【收藏项目】", "分页列表应显示收藏项目分组")
    assert_contains(text, "【最近项目】", "分页列表应显示最近项目分组")
    buttons = markup["inline_keyboard"]
    flat_labels = [button["text"] for row in buttons for button in row]
    assert_true(any("☆ 收藏" in label or "⭐ 取消收藏" in label for label in flat_labels), "列表中应提供收藏切换按钮")
    assert_true(any("📍 置顶" in label or "📌 取消置顶" in label for label in flat_labels), "列表中应提供置顶切换按钮")
    assert_true(any("下一页" in label for label in flat_labels), "超出单页容量时应提供分页按钮")


async def test_exit_closes_cli_tab() -> None:
    """测试交互 CLI 退出时会自动关闭当前绑定 tab。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    iterm._set_foreground_state(iterm.target_session, SCREEN_TEXT_PROMPT_ID, "opencode")
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    app._session_mode = SessionMode.OPENCODE
    await app._exit_cli_mode(TEST_CHAT_ID)
    assert_equal(iterm.closed_target_tab_count, 1, "/exit 后应自动关闭当前绑定的 CLI tab")
    assert_equal(iterm.foreground_state, None, "关闭 CLI tab 后应清理前台状态")
    assert_equal(app._session_mode, SessionMode.SHELL, "退出后应回到 Shell 模式")


async def test_opencode_exit_then_shell_output() -> None:
    """测试 OpenCode 退出后 shell 命令不会因为残留前台状态而空输出。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    iterm.simulate_stale_foreground_empty_output = True
    iterm._set_foreground_state(iterm.target_session, SCREEN_TEXT_PROMPT_ID, "opencode")
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    app._session_mode = SessionMode.OPENCODE
    await app._exit_cli_mode(TEST_CHAT_ID, silent=True)
    await app._dispatch_text(TEST_CHAT_ID, "pwd")
    await asyncio.sleep(0)
    if app._command_task is not None:
        await app._command_task
    last_message = telegram.edits[-1][2] if telegram.edits else telegram.messages[-1][1]
    assert_true("暂无输出" not in last_message, "退出 OpenCode 后 shell 命令不应再出现空输出")


async def test_claude_mode_creates_new_tab() -> None:
    """测试进入 Claude 模式时会先新建并绑定独立 tab。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    app._wait_for_claude_startup_state = lambda session, timeout=20.0: maybe_await("ready")  # type: ignore[method-assign]
    await app._enter_cli_mode(TEST_CHAT_ID, SessionMode.CLAUDE, "")
    assert_equal(iterm.created_tab_count, 1, "进入 Claude 模式前应先新建一个 tab")
    assert_equal(iterm.created_tab_activate_flags, [False], "Claude 模式应在后台新建 tab，避免切走当前可见 tab")
    assert_true(bool(iterm.target_session.sent_texts), "新 tab 内应启动 Claude CLI")
    assert_contains(iterm.target_session.sent_texts[0], "claude", "启动命令应包含 claude")
    assert_true(iterm.target_session.sent_texts[0].endswith("\r"), "Claude 启动命令应带回车发送")
    assert_equal(
        iterm.foreground_state,
        {
            "session": iterm.target_session,
            "prompt_id": SCREEN_TEXT_PROMPT_ID,
            "command_name": "claude",
        },
        "Claude 前台状态应绑定到整屏 session",
    )
    assert_contains(telegram.messages[-1][1], "当前绑定 tab：1", "进入 Claude 模式时应回显绑定的 tab 编号")


async def test_opencode_mode_creates_new_tab() -> None:
    """测试进入 OpenCode 模式时会先新建并绑定独立 tab。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._enter_opencode_interactive_mode(TEST_CHAT_ID, "")
    assert_equal(iterm.created_tab_count, 1, "进入 OpenCode 模式前应先新建一个 tab")
    assert_equal(iterm.created_tab_activate_flags, [False], "OpenCode 模式应在后台新建 tab，避免切走当前可见 tab")
    assert_true(bool(iterm.target_session.sent_texts), "新 tab 内应启动 OpenCode CLI")
    assert_contains(iterm.target_session.sent_texts[0], "opencode -c", "启动命令应包含 opencode 上下文参数")
    assert_true(iterm.target_session.sent_texts[0].endswith("\r"), "OpenCode 启动命令应带回车发送")
    assert_contains(telegram.messages[-1][1], "当前绑定 tab：1", "进入 OpenCode 模式时应回显绑定的 tab 编号")
    assert_equal(
        iterm.foreground_state,
        {
            "session": iterm.target_session,
            "prompt_id": SCREEN_TEXT_PROMPT_ID,
            "command_name": "opencode",
        },
        "OpenCode 前台状态应绑定到新建 tab 的 session",
    )


async def test_cursor_mode_uses_screen_binding() -> None:
    """测试进入 Cursor 模式时会绑定整屏前台状态。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
        telegram=telegram,  # type: ignore[arg-type]
        iterm=iterm,  # type: ignore[arg-type]
    )
    await app._enter_cli_mode(TEST_CHAT_ID, SessionMode.CURSOR, "")
    assert_equal(iterm.created_tab_count, 1, "进入 Cursor 模式前应先新建一个 tab")
    assert_equal(iterm.created_tab_activate_flags, [False], "Cursor 模式应在后台新建 tab，避免切走当前可见 tab")
    assert_true(bool(iterm.target_session.sent_texts), "新 tab 内应启动 Cursor CLI")
    assert_contains(iterm.target_session.sent_texts[0], "agent", "启动命令应包含 agent")
    assert_true(iterm.target_session.sent_texts[0].endswith("\r"), "Cursor 启动命令应带回车发送")
    assert_equal(
        iterm.foreground_state,
        {
            "session": iterm.target_session,
            "prompt_id": SCREEN_TEXT_PROMPT_ID,
            "command_name": "agent",
        },
        "Cursor 前台状态应直接绑定到整屏 session",
    )


async def test_cursor_double_enter_submit() -> None:
    """测试 Cursor Agent 发送时会追加双回车以稳定提交。"""
    controller = SubmitProbeController("agent")

    async def on_update(_output: str) -> None:
        return None

    await controller.send_foreground_input_stream(
        text="hello",
        on_update=on_update,
        stream_interval=0.1,
    )
    assert_equal(
        controller.probe_session.sent_texts,
        ["hello", "\r", "\r"],
        "Cursor Agent 输入应发送文本后双回车",
    )


def test_claude_invalid_resume_error() -> None:
    """测试 Claude 失效 resume 错误识别。"""
    assert_equal(
        _is_invalid_claude_resume_error("No conversation found with session ID: 123"),
        True,
        "应识别 Claude 不存在的 session ID 错误",
    )
    assert_equal(
        _is_invalid_claude_resume_error("some other error"),
        False,
        "其他错误不应被误判为 resume 失效",
    )


async def test_claude_silent_invalid_resume_fallback() -> None:
    """测试 Claude 静默模式遇到失效会话时会自动重试新会话。"""
    old_run_subprocess = bot_module._run_subprocess
    old_read_session_id = bot_module._read_session_id
    old_save_session_id = bot_module._save_session_id
    old_clear_session_id = bot_module._clear_session_id
    old_set_active_marker = bot_module._set_active_marker
    old_sync_session_id_from_marker = bot_module._sync_session_id_from_marker
    calls: list[list[str]] = []
    cleared: list[SessionMode] = []
    saved: list[tuple[SessionMode, str]] = []

    async def fake_run_subprocess(args: list[str], timeout: float = 600.0) -> str:
        _ = timeout
        calls.append(args)
        if "--resume" in args:
            raise RuntimeError("No conversation found with session ID: dead-session")
        return "fresh ok"

    try:
        bot_module._run_subprocess = fake_run_subprocess
        bot_module._read_session_id = lambda mode: "dead-session" if mode == SessionMode.CLAUDE else None
        bot_module._save_session_id = lambda mode, session_id: saved.append((mode, session_id))
        bot_module._clear_session_id = lambda mode: cleared.append(mode)
        bot_module._set_active_marker = lambda mode, active=True: None
        bot_module._sync_session_id_from_marker = lambda mode: None

        app = Tg2ITermApp(
            config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
            telegram=FakeTelegram(),  # type: ignore[arg-type]
            iterm=FakeITerm(),  # type: ignore[arg-type]
        )
        output = await app._run_claude_silent("hello")
        assert_equal(output, "fresh ok", "失效会话后应自动回退到新会话执行")
        assert_equal(cleared, [SessionMode.CLAUDE], "失效 resume 时应清掉已保存的 Claude session")
        assert_true(any("--resume" in call for call in calls), "应先尝试 resume")
        assert_true(any("--session-id" in call for call in calls), "resume 失败后应重试新会话")
        assert_equal(saved[-1][0], SessionMode.CLAUDE, "新会话成功后应保存 Claude session")
    finally:
        bot_module._run_subprocess = old_run_subprocess
        bot_module._read_session_id = old_read_session_id
        bot_module._save_session_id = old_save_session_id
        bot_module._clear_session_id = old_clear_session_id
        bot_module._set_active_marker = old_set_active_marker
        bot_module._sync_session_id_from_marker = old_sync_session_id_from_marker


async def test_claude_interactive_invalid_resume_fallback() -> None:
    """测试 Claude 交互模式遇到失效会话时会自动切换到新会话。"""
    old_read_session_id = bot_module._read_session_id
    old_clear_session_id = bot_module._clear_session_id
    old_set_active_marker = bot_module._set_active_marker
    cleared: list[SessionMode] = []
    telegram = FakeTelegram()
    iterm = FakeITerm()
    send_calls: list[str] = []

    startup_states = iter(["invalid_resume", "ready"])

    async def fake_wait_for_claude_startup_state(_session: Any, timeout: float = 20.0) -> str:
        _ = timeout
        return next(startup_states)

    async def fake_send_to_cli(_chat_id: int, text: str) -> None:
        send_calls.append(text)

    try:
        bot_module._read_session_id = lambda mode: "dead-session" if mode == SessionMode.CLAUDE else None
        bot_module._clear_session_id = lambda mode: cleared.append(mode)
        bot_module._set_active_marker = lambda mode, active=True: None

        app = Tg2ITermApp(
            config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5, "/tmp/tg2iterm2_reminders.db"),
            telegram=telegram,  # type: ignore[arg-type]
            iterm=iterm,  # type: ignore[arg-type]
        )
        app._wait_for_claude_startup_state = fake_wait_for_claude_startup_state  # type: ignore[method-assign]
        app._send_to_cli = fake_send_to_cli  # type: ignore[method-assign]
        await app._enter_cli_mode(TEST_CHAT_ID, SessionMode.CLAUDE, "hello")
        assert_equal(cleared, [SessionMode.CLAUDE], "交互模式 resume 失效时应清掉保存的 Claude session")
        assert_equal(len(iterm.target_session.sent_texts), 2, "交互模式应在 resume 失败后立即重启新会话")
        assert_contains(iterm.target_session.sent_texts[0], "--resume dead-session", "第一次应先尝试恢复旧会话")
        assert_equal(iterm.target_session.sent_texts[1], "claude\r", "恢复失败后应直接启动全新 Claude 会话")
        assert_equal(send_calls, ["hello"], "首条消息应在新会话就绪后再发送给 Claude")
        assert_contains(
            "\n".join(text for _chat_id, text in telegram.messages),
            "已自动切换到新会话",
            "恢复失败后应提示用户已自动切到新会话",
        )
    finally:
        bot_module._read_session_id = old_read_session_id
        bot_module._clear_session_id = old_clear_session_id
        bot_module._set_active_marker = old_set_active_marker


def test_opencode_input_anchor() -> None:
    """测试 OpenCode 的 `┃` 输入回显行也能作为本轮锚点。"""
    before = (
        "  ┃  hello\n"
        "  ┃\n\n"
        "     Hello.\n\n"
        "     ▣  Build · GPT-5.4 · 3.1s\n"
    )
    current = (
        before
        + "  ┃  hello\n"
        + "  ┃\n\n"
        + "  ┃  Thinking: Responding to greetings\n"
        + '  ┃  I need to provide a simple greeting.\n\n'
        + "     Hello.\n\n"
        + "     ▣  Build · GPT-5.4 · 4.2s\n"
        + "  ╹▀▀▀▀▀▀▀▀▀▀▀▀\n"
        + "   ⬝⬝⬝⬝⬝■■■  esc interrupt\n"
    )
    delta = output_after(before, current, "hello")
    assert_contains(delta, "Thinking: Responding to greetings", "应从最新一轮 OpenCode 输入之后开始截取")
    cleaned = _clean_opencode_delta(delta)
    assert_equal(cleaned, "Hello.", "最终输出应只保留 OpenCode 正文回答")


def test_opencode_delta_clean() -> None:
    """测试 OpenCode 清理逻辑会移除侧栏和状态栏噪声。"""
    delta = (
        "  ┃  hello\n"
        "  ┃\n\n"
        "  ┃  Thinking: Responding to greetings\n"
        "  ┃  I need to provide a simple greeting.\n\n"
        "     Hello.\n\n"
        "     ▣  Build · GPT-5.4 · 4.2s\n"
        "  ╹▀▀▀▀▀▀▀▀▀▀▀▀\n"
        "   ⬝⬝⬝⬝⬝■■■  esc interrupt\n"
    )
    cleaned = _clean_opencode_delta(delta)
    assert_equal(cleaned, "Hello.", "OpenCode 最终输出应去掉输入栏和思考栏")
    assert_equal(_has_opencode_answer(delta), True, "含正文回答时应识别为 OpenCode 已有答案")


def test_claude_repeated_input_anchor() -> None:
    """测试答案重复用户输入时不从答案中间截断。"""
    before = "❯ old\n\n⏺ old answer\n\n✻ Sautéed for 2s\n\n❯\n"
    current = (
        before
        + '❯ 12-22\n\n'
        + '⏺ I\'m not sure what you\'re asking. Could you clarify what you\'d like me to do with "12-22"? For example:\n'
        + "  - Calculate 12 - 22 (= -10)?\n"
        + "  - Look up something dated Dec 22?\n"
        + "  - Something else?\n\n"
        + "✻ Worked for 5s\n"
    )
    delta = output_after(before, current, "12-22")
    assert_true(delta.startswith("⏺ I'm not sure"), "输出应从 Claude 回答开头开始")
    assert_contains(delta, '"12-22"?', "答案里重复用户输入时不应截断")
    assert_contains(delta, "Something else?", "答案尾部应保留")


def test_claude_empty_before_no_history() -> None:
    """测试 before_output 为空时不能返回旧历史。"""
    previous_output = "❯ 只回答数字: 17*19 等于多少?\n\n⏺ 323\n\n✻ Cooked for 2s\n\n❯\n"
    assert_equal(
        output_after("", previous_output, "数值计算"),
        "",
        "没有本轮输入锚点时不能把上一轮输出当成本轮结果",
    )


def test_cursor_pasted_input_anchor() -> None:
    """测试 Cursor 的 iTerm2 粘贴回显行仍能定位本轮输入锚点。"""
    before = "欢迎来到 Cursor\n\n>\n"
    current = (
        before
        + "[Pasted ~4 linehello\n\n"
        + "  你好，Hello。今天想聊点什么，还是有什么任务要一起处理？\n\n"
        + ">\n"
    )
    delta = output_after(before, current, "hello")
    assert_contains(delta, "你好，Hello。", "粘贴回显前缀后仍应提取到 Cursor 回答")
    assert_not_contains(delta, "[Pasted", "本轮输出不应把粘贴回显噪声带给 Telegram")


def test_cursor_follow_up_complete() -> None:
    """测试 Cursor 回到 follow-up UI 时应视为完成，并清理 reminder 噪声。"""
    screen = (
        "[Pasted ~11hello\n\n"
        "  你好。有什么我可以帮你的？\n\n"
        "<system-reminder>\n"
        "Your operational mode has changed from plan to build.\n"
        "You are no longer in read-only mode.\n"
        "You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed.\n"
        "</system-reminder>\n\n"
        "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
        "  → Add a follow-up\n"
        "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
        "  Composer 2 Fast · 24.5%                                                                                                                                                                            Auto-run\n"
        "  ~\n"
    )
    delta = output_after("", screen, "hello")
    assert_true(_has_cursor_ready_state(screen), "出现 follow-up UI 时应认为 Cursor 已回到可继续追问状态")
    assert_true(_has_cursor_answer(delta), "真实回答样例应被识别为已有答案")
    cleaned = _clean_generic_delta(delta)
    assert_contains(cleaned, "你好。有什么我可以帮你的？", "应保留 Cursor 的正文回答")
    assert_not_contains(cleaned, "system-reminder", "最终输出不应包含 system-reminder 标签")
    assert_not_contains(cleaned, "Add a follow-up", "最终输出不应包含后续追问 UI")
    assert_not_contains(cleaned, "Composer 2 Fast", "最终输出不应包含底部状态栏")


def test_cursor_system_reminder_not_answer() -> None:
    """测试只有 system-reminder 时不能算 Cursor 回答。"""
    delta = (
        "<system-reminder>\n"
        "Your operational mode has changed from plan to build.\n"
        "You are no longer in read-only mode.\n"
        "You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed.\n"
        "</system-reminder>\n"
    )
    assert_equal(_clean_generic_delta(delta), "", "system-reminder 不应成为最终输出")
    assert_equal(_has_cursor_answer(delta), False, "system-reminder 不能被当成 Cursor 正文回答")


def test_cursor_composer_status_clean() -> None:
    """测试 Cursor 的 Composer 百分比状态栏不会进入最终回答。"""
    delta = (
        "你好，有什么需要帮忙的吗？可以是写代码、查文档、调试问题，或其他技术相关的事情。\n\n"
        "  Composer 2 Fast · 12.7%\n"
        "  ~\n"
    )
    cleaned = _clean_generic_delta(delta)
    assert_equal(
        cleaned,
        "你好，有什么需要帮忙的吗？可以是写代码、查文档、调试问题，或其他技术相关的事情。",
        "Composer 百分比状态栏不应污染最终 Cursor 输出",
    )


async def test_prompt_output_settle() -> None:
    """测试 output_range 写入过程中的半截输出不会被立即返回。"""
    partial = "⏺ 我当前可用的 skills：\n\n  1. claude-hud:setup\n  2. claude-hud:configure\n"
    full = partial + "  3. andrej-karpathy-skills:karpathy-guidelines\n  4. update-config\n"
    controller = SequenceOutputController(
        ["", partial, partial, partial, full, full, full, full]
    )
    output = await controller._read_current_command_output_after_settle(None, "prompt-1")  # type: ignore[arg-type]
    assert_equal(output, full, "应返回稳定后的完整 output_range")


def test_claude_long_answer_preserve() -> None:
    """测试 Claude 多行回答不会被清理逻辑截短。"""
    delta = (
        "⏺ 我当前可用的 skills：\n\n"
        "  1. claude-hud:setup - 配置 claude-hud 作为状态栏\n"
        "  2. claude-hud:configure - 配置 HUD 显示选项\n"
        "  3. andrej-karpathy-skills:karpathy-guidelines - 减少编码错误\n"
        "  4. update-config - 通过 settings.json 配置 Claude Code\n"
        "  5. keybindings-help - 自定义键盘快捷键\n"
        "  6. simplify - 审查修改过的代码\n"
        "  7. fewer-permission-prompts - 减少权限提示\n"
        "  8. loop - 按固定间隔重复运行 prompt\n"
        "  9. claude-api - 构建、调试、优化 Claude API\n"
        "  10. init - 初始化 CLAUDE.md 文件\n"
        "  11. review - 审查 pull request\n"
        "  12. security-review - 安全审查\n\n"
        "  需要使用哪个？\n\n"
        "✻ Sautéed for 11s\n\n"
        "❯\n"
    )
    cleaned = clean_claude_delta(delta)
    assert_contains(cleaned, "1. claude-hud:setup", "长回答应保留第 1 行")
    assert_contains(cleaned, "12. security-review", "长回答应保留第 12 行")
    assert_contains(cleaned, "需要使用哪个？", "长回答尾部正文应保留")


def test_claude_nul_space_preserve() -> None:
    """测试 iTerm2 空白占位符保留为空格。"""
    delta = "⏺  - Something\x00else?\n\n✻ Worked for 5s\n\n❯\n"
    cleaned = clean_claude_delta(delta)
    assert_contains(cleaned, "Something else?", "NUL 占位符应转为空格")
    assert_true("Somethingelse" not in cleaned, "NUL 占位符不能被直接删除")


def test_claude_thinking_not_complete() -> None:
    """测试 Claude 思考状态不能误判完成。"""
    delta = "⏺ 5663 - 22 = 5641\n\n✻ Crunched for 7s\n"
    screen = "❯ 5663-22\n\n" + delta
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯ ",
            cursor_x=2,
        ),
        False,
        "屏幕底部还是状态行时不能返回已完成",
    )


def test_claude_tool_running_not_complete() -> None:
    """测试 Claude 工具仍在运行时不能因为 prompt 可见而误判完成。"""
    delta = (
        "⏺ I understand you want to discuss numerical computing in Chinese.\n\n"
        "  Explore(List numerical computation files)\n"
        "  ⎿  Bash(find /Users/luca -type d -name \"*numerical*\")\n"
        "     Running…\n"
        "     Bash(ls -la /Users/luca/ | head -20)\n"
        "     Running…\n"
        "     (ctrl+b to run in background)\n\n"
        "✽ Swooping… (26s · ↓ 510 tokens)\n"
        "  ⎿  Tip: Hit shift+tab to cycle between default mode\n\n"
        "────────────────────────────────────────────────────────\n"
        "❯\n"
    )
    screen = "❯ 数值计算\n\n" + delta
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯ ",
            cursor_x=2,
        ),
        False,
        "回答后仍有 Running 工具和活跃 spinner 时不能返回已完成",
    )


def test_claude_spinner_tip_not_complete() -> None:
    """测试 Claude spinner 和 tip 不能作为最终答案。"""
    delta = "Orchestrating...\n\ntip: press Shift+Tab to cycle modes\n"
    screen = "❯ 数值计算\n\n" + delta + "\n❯\n"
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯ ",
            cursor_x=2,
        ),
        False,
        "只有 spinner/tip 时即使 prompt 可见也不能返回已完成",
    )
    assert_equal(clean_claude_delta(delta), "", "spinner/tip 不应成为最终答案")


def test_claude_new_spinner_tip_not_complete() -> None:
    """测试新版 Claude spinner 和 tip 不能作为最终答案。"""
    delta = "✽ Spelunking… (2s · ↓ 2 tokens)\n  ⎿  Tip: Did you know you can drag and drop image files into your terminal?\n"
    screen = "❯ 数值计算\n\n" + delta + "\n❯\n"
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯ ",
            cursor_x=2,
        ),
        False,
        "新版 spinner/tip 没有回答正文时不能完成",
    )
    assert_equal(clean_claude_delta(delta), "", "新版 spinner/tip 不应成为最终答案")


def test_claude_prompt_complete() -> None:
    """测试 Claude 回到 prompt 后完成。"""
    delta = "Frosting...\n\n⏺ 5663 - 22 = 5641\n\n✻ Crunched for 7s\n"
    screen = "❯ 5663-22\n\n" + delta + "\n❯"
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯ ",
            cursor_x=2,
        ),
        True,
        "底部 prompt 和光标都就绪时应完成",
    )


def test_claude_cursor_position() -> None:
    """测试 Claude prompt 光标必须在右侧。"""
    assert_equal(is_claude_prompt_cursor("❯", 0), False, "光标在符号上不算可输入")
    assert_equal(is_claude_prompt_cursor("❯", 1), True, "光标在符号右侧才算可输入")
    assert_equal(is_claude_prompt_cursor("❯ ", 2), True, "光标在空格右侧也算可输入")


def test_claude_unknown_progress_clean() -> None:
    """测试未知 Claude 进度词最终清理。"""
    delta = "Whatchamacalliting...\n\n⏺ 4\n\n✻ Cogitated for 11s\n\n❯\n"
    assert_equal(clean_claude_delta(delta), "4", "未知进度词不应污染最终答案")


def test_claude_real_screen_tail() -> None:
    """测试真实 Claude TUI 屏幕里的填充字符和尾部分隔线。"""
    delta = (
        "⏺ 323\n"
        "\x00\x00\x00\n"
        "✻ Cooked for 2s\n\n"
        "────────────────────────────────────────────────────────\n"
        "❯\xa0 \x00\x00\x00\n"
        "────────────────────────────────────────────────────────\n"
    )
    screen = "❯ 只回答数字: 17*19 等于多少?\n\n" + delta
    assert_equal(
        is_claude_turn_complete(
            delta,
            screen_text=screen,
            cursor_line="❯\xa0 \x00\x00\x00",
            cursor_x=2,
        ),
        True,
        "真实 Claude 屏幕完成态应忽略填充字符和 prompt 下方分隔线",
    )
    assert_equal(clean_claude_delta(delta), "323", "真实 Claude 屏幕最终应只保留答案")


def test_hook_signal_detection() -> None:
    """测试 hook 信号文件检测逻辑。"""
    signal_path = Path(tempfile.mktemp(prefix="tg2iterm2_test_signal_"))
    controller = ITermController(claude_done_signal=str(signal_path))
    assert_equal(
        controller._read_hook_signal_ns(),
        None,
        "信号文件不存在时应返回 None",
    )
    before_ns = 1000000000000000000
    signal_path.write_text(str(before_ns - 1))
    assert_equal(
        controller._read_hook_signal_ns(),
        before_ns - 1,
        "信号文件时间戳小于 before_ns 时应正确读取",
    )
    after_ns = before_ns + 500000000
    signal_path.write_text(str(after_ns))
    result = controller._read_hook_signal_ns()
    assert_equal(result, after_ns, "信号文件时间戳更新后应读取到新值")
    assert_true(
        result is not None and result > before_ns,
        "信号时间戳应大于发送时间",
    )
    signal_path.unlink(missing_ok=True)


def restore_env(old_values: dict[str, str | None]) -> None:
    """恢复测试前的环境变量。"""
    for name, value in old_values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class suppress_expected:
    """断言指定异常必须出现。"""

    def __init__(self, error_type: type[BaseException], message: str) -> None:
        """保存预期异常类型和错误提示。"""
        self._error_type = error_type
        self._message = message

    def __enter__(self) -> None:
        """进入异常断言上下文。"""
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> bool:
        """检查异常是否符合预期。"""
        if exc is None:
            raise AssertionError(self._message)
        if not isinstance(exc, self._error_type):
            return False
        return True


async def run_cases(cases: list[TestCase]) -> None:
    """按顺序运行测试用例并打印场景。"""
    for index, case in enumerate(cases, start=1):
        print(f"[{index:02d}] 场景: {case.scenario}")
        await maybe_await(case.func())
        print(f"     结果: PASS ({case.name})")


async def run_iterm_safe_tests() -> None:
    """在新建 iTerm2 tab 中运行 pwd 和 hostname 安全集成测试。"""
    controller = ITermController()
    await controller.connect()
    tab_number = await controller.create_new_tab()
    tab_unique_id = controller._default_tab_unique_id
    print(f"[ITERM] 场景: 新建测试 tab 编号 {tab_number} 后执行 pwd")
    try:
        await wait_until_shell_ready(controller, timeout=30)
        pwd_result = await run_safe_terminal_command(controller, "pwd")
        assert_equal(pwd_result.exit_status, 0, "pwd 应正常退出")
        assert_true(
            any(line.strip().startswith("/") for line in pwd_result.output.splitlines()),
            f"pwd 输出中应包含绝对路径行，实际输出: {pwd_result.output!r}",
        )
        print("        结果: PASS (pwd)")

        print("[ITERM] 场景: 在同一测试 tab 执行 hostname")
        hostname_result = await run_safe_terminal_command(controller, "hostname")
        assert_equal(hostname_result.exit_status, 0, "hostname 应正常退出")
        assert_true(
            bool(hostname_result.output.strip()),
            f"hostname 输出不能为空，实际输出: {hostname_result.output!r}",
        )
        print(f"        结果: PASS (hostname={hostname_result.output.strip()!r})")
    finally:
        await close_test_tab(controller, tab_unique_id)


async def run_safe_terminal_command(controller: ITermController, command: str) -> Any:
    """执行一个安全终端命令并设置超时。"""
    updates: list[str] = []

    async def on_update(output: str) -> None:
        """记录流式输出。"""
        updates.append(output)

    return await asyncio.wait_for(
        controller.run_command_stream(
            command=command,
            on_update=on_update,
            stream_interval=0.2,
        ),
        timeout=30,
    )


async def run_claude_safe_test() -> None:
    """在新建 iTerm2 tab 中运行 Claude 数值计算集成测试。"""
    if shutil.which("claude") is None:
        raise AssertionError("未找到 claude 命令，无法运行 Claude 集成测试")

    controller = ITermController()
    await controller.connect()
    tab_number = await controller.create_new_tab()
    tab_unique_id = controller._default_tab_unique_id
    claude_task: asyncio.Task[Any] | None = None
    print(f"[CLAUDE] 场景: 新建测试 tab 编号 {tab_number} 启动 Claude 并计算 17*19")
    try:
        await wait_until_shell_ready(controller, timeout=30)

        async def on_claude_update(_output: str) -> None:
            """接收 Claude 主进程流式输出。"""
            return None

        claude_task = asyncio.create_task(
            controller.run_command_stream(
                command="claude",
                on_update=on_claude_update,
                stream_interval=0.5,
            )
        )
        await wait_until_claude_ready(controller, timeout=90)

        interaction_updates: list[str] = []

        async def on_interaction_update(output: str) -> None:
            """记录 Claude 单轮交互流式输出。"""
            interaction_updates.append(output)

        output = await asyncio.wait_for(
            controller.send_foreground_input_stream(
                text="只回答数字: 17*19 等于多少?",
                on_update=on_interaction_update,
                stream_interval=0.5,
            ),
            timeout=240,
        )
        assert_contains(output, "323", "Claude 数值计算结果应包含 323")
        print("         结果: PASS (17*19=323)")

        print("[CLAUDE] 场景: Claude 内输入包含“数值计算”的中文请求，最终不能返回 spinner/tip")
        topic_output = await asyncio.wait_for(
            controller.send_foreground_input_stream(
                text="只回复这句话: 数值计算测试通过",
                on_update=on_interaction_update,
                stream_interval=0.5,
            ),
            timeout=240,
        )
        assert_contains(topic_output, "数值", "Claude 主题回复应包含用户输入主题")
        assert_not_contains(topic_output, "323", "最终输出不应沿用上一轮计算结果")
        assert_not_contains(topic_output, "Orchestrating", "最终输出不应是 spinner")
        assert_not_contains(topic_output.lower(), "tip:", "最终输出不应是提示行")
        print("         结果: PASS (数值计算)")
    finally:
        with suppress(Exception):
            await controller.send_ctrl_c()
        await close_test_tab(controller, tab_unique_id)
        if claude_task is not None:
            claude_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await claude_task


async def wait_until_claude_ready(
    controller: ITermController,
    timeout: float,
) -> None:
    """等待 Claude TUI 出现空输入提示符。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        lines = (await controller.read_last_lines(20)).splitlines()
        if any(line.strip() in {"❯", ">"} for line in lines):
            return
        await asyncio.sleep(0.5)
    raise AssertionError("等待 Claude 输入提示符超时")


async def wait_until_shell_ready(
    controller: ITermController,
    timeout: float,
) -> None:
    """等待新建 iTerm2 tab 中的 shell prompt 就绪。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_text = ""
    while loop.time() < deadline:
        last_text = await controller.read_last_lines(10)
        if any(is_shell_prompt_line(line) for line in last_text.splitlines()):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"等待 shell prompt 超时，最后屏幕内容: {last_text!r}")


def is_shell_prompt_line(line: str) -> bool:
    """判断一行是否像 zsh/bash 的空 shell prompt。"""
    stripped = line.replace("\x00", "").replace("\xa0", " ").strip()
    return stripped.endswith("%") or stripped.endswith("$") or stripped.endswith("#")


async def close_test_tab(
    controller: ITermController,
    tab_unique_id: str | None,
) -> None:
    """关闭集成测试创建的 iTerm2 tab。"""
    if not tab_unique_id:
        return
    tab = await controller._find_tab_by_unique_id(tab_unique_id)
    if tab is not None:
        await tab.async_close(force=True)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="运行 tg2iterm2 本地测试")
    parser.add_argument(
        "--iterm",
        action="store_true",
        help="额外运行 iTerm2 安全集成测试: pwd、hostname",
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="额外运行 Claude 安全集成测试: 17*19 数值计算",
    )
    return parser.parse_args()


async def main() -> None:
    """运行测试主入口。"""
    args = parse_args()
    old_command_usage_file = bot_module.COMMAND_USAGE_FILE
    with tempfile.TemporaryDirectory(prefix="tg2iterm2_test_") as temp_dir:
        bot_module.COMMAND_USAGE_FILE = Path(temp_dir) / "command_usage.json"
        try:
            await run_cases(build_unit_cases())
            if args.iterm:
                await run_iterm_safe_tests()
            if args.claude:
                await run_claude_safe_test()
        finally:
            bot_module.COMMAND_USAGE_FILE = old_command_usage_file
    print("全部测试通过")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        raise
