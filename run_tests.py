#!/Users/luca/miniforge3/envs/py311/bin/python
"""tg2iterm2 本地测试入口。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import shutil
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bot_app import Tg2ITermApp, render_stream_message
from config import AppConfig, load_config
from iterm_controller import (
    ITermController,
    CLAUDE_DONE_SIGNAL_DEFAULT,
    clean_claude_delta,
    command_name,
    is_claude_prompt_cursor,
    is_claude_turn_complete,
    output_after,
    parse_tab_number,
)
from telegram_client import limit_telegram_text, sanitize_filename


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


class FakeITerm:
    """记录测试中的 iTerm2 调用。"""

    def __init__(self) -> None:
        """初始化命令列表。"""
        self.commands: list[str] = []
        self.enter_count = 0

    async def run_command_stream(
        self,
        command: str,
        on_update: Callable[[str], Any],
        stream_interval: float,
    ) -> Any:
        """记录普通命令输入。"""
        self.commands.append(command)
        await maybe_await(on_update("fake output"))
        return type("Result", (), {"exit_status": 0, "output": "fake output"})()

    async def send_enter(self) -> None:
        """记录发送回车。"""
        self.enter_count += 1


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
            name="enter_command",
            scenario="控制命令: /enter 只发送回车键，不走普通命令执行",
            func=test_enter_command,
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
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5),
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
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5),
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
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5),
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


async def test_enter_command() -> None:
    """测试 /enter 只发送回车。"""
    telegram = FakeTelegram()
    iterm = FakeITerm()
    app = Tg2ITermApp(
        config=AppConfig("dummy", TEST_CHAT_ID, None, 0.1, "/tmp/tg2iterm2_claude_done", 300.0, "/tmp/tg2iterm2_perm_request.json", "/tmp/tg2iterm2_perm_response.json", 0.5),
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


def test_render_stream_message() -> None:
    """测试 Telegram 命令响应格式。"""
    running = render_stream_message("pwd", "/tmp/workspace", finished=False)
    done = render_stream_message("echo", "hello-test-output", finished=True, exit_status=0)
    assert_contains(running, "执行中", "执行中消息应标明状态")
    assert_contains(running, "/tmp/workspace", "执行中消息应包含输出内容")
    assert_contains(done, "已完成 exit=0", "完成消息应包含退出码")
    assert_contains(done, "hello-test-output", "完成消息应包含输出")


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
    await run_cases(build_unit_cases())
    if args.iterm:
        await run_iterm_safe_tests()
    if args.claude:
        await run_claude_safe_test()
    print("全部测试通过")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        raise
