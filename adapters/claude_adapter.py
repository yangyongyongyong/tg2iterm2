"""Claude CLI 交互适配器。

将 iterm_controller.py 中的 Claude TUI 检测逻辑、hook 信号读取
和输出清理函数集中到此处，作为 InteractiveAdapter 的具体实现。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from adapters.base import InteractiveAdapter, SlashCommand


CLAUDE_DONE_SIGNAL_DEFAULT = Path("/tmp/tg2iterm2_claude_done")

CLAUDE_STATUS_RE = re.compile(r"^\s*✻\s+.+\s+for\s+\d+(?:\.\d+)?s\s*$")
CLAUDE_TIP_RE = re.compile(r"^\s*(?:⎿\s*)?tip:\s+.+$", re.IGNORECASE)
CLAUDE_SPINNER_RE = re.compile(r"^\s*[✽✻]\s+.+(?:\.{3}|…)\s*(?:\(.*\))?\s*$")

BUILTIN_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("init", "初始化 CLAUDE.md 文件"),
    ("review", "审查 Pull Request"),
    ("security-review", "安全审查"),
    ("simplify", "审查代码质量和效率"),
    ("fewer-permission-prompts", "减少权限弹窗"),
    ("loop", "循环执行命令"),
    ("update-config", "配置 settings.json"),
    ("schedule", "定时任务调度"),
    ("claude-api", "Claude API 开发调试"),
]


class ClaudeAdapter(InteractiveAdapter):
    """Claude CLI 交互适配器。"""

    def __init__(
        self,
        done_signal: str | None = None,
        hook_timeout: float = 300.0,
    ) -> None:
        """初始化 Claude 适配器。

        Args:
            done_signal: Claude Stop hook 信号文件路径。
            hook_timeout: hook 等待超时时间（秒）。
        """
        self._done_signal = Path(done_signal) if done_signal else CLAUDE_DONE_SIGNAL_DEFAULT
        self._hook_timeout = hook_timeout

    @property
    def name(self) -> str:
        """适配器名称。"""
        return "claude"

    @property
    def cli_command(self) -> str:
        """启动 Claude CLI 的命令。"""
        return "claude"

    def is_turn_complete(
        self,
        delta: str,
        screen_text: str,
        cursor_line: str,
        cursor_x: int,
    ) -> bool:
        """判断 Claude TUI 单轮交互是否已经回到可输入状态。"""
        return (
            has_claude_answer(delta)
            and not has_claude_active_work_after_answer(delta)
            and not has_claude_active_work_after_answer(screen_text)
            and is_claude_prompt_cursor(cursor_line, cursor_x)
            and has_claude_ready_prompt_tail(screen_text)
        )

    def clean_output(self, delta: str) -> str:
        """移除 Claude TUI 尾部状态行、分隔线和输入提示符。"""
        return clean_claude_delta(delta)

    def has_answer(self, delta: str) -> bool:
        """判断本轮输出是否已经包含 Claude 回答正文。"""
        return has_claude_answer(delta)

    def get_slash_commands(self) -> list[SlashCommand]:
        """返回 Claude 可用的斜杠命令列表，按名称排序。"""
        cmds = [SlashCommand(name=n, description=d) for n, d in BUILTIN_SLASH_COMMANDS]
        cmds.sort(key=lambda c: c.name)
        return cmds

    def get_done_signal_path(self) -> str | None:
        """返回 Claude Stop hook 信号文件路径。"""
        return str(self._done_signal)

    def read_hook_signal_ns(self) -> int | None:
        """读取 hook 信号文件中的纳秒时间戳，失败返回 None。"""
        try:
            content = self._done_signal.read_text().strip()
            return int(content)
        except (OSError, ValueError):
            return None


def normalize_terminal_line(line: str) -> str:
    """移除 iTerm2/TUI 读取出来的填充字符并裁剪空白。"""
    return line.replace("\x00", "").replace("\xa0", " ").strip()


def is_separator_line(stripped_line: str) -> bool:
    """判断一行是否只是 Claude TUI 的横向分隔线。"""
    return bool(stripped_line) and set(stripped_line) <= {"─", "-"}


def has_claude_answer(delta: str) -> bool:
    """判断本轮输出是否已经包含 Claude 回答正文。"""
    return any(normalize_terminal_line(line).startswith("⏺") for line in delta.splitlines())


def has_claude_ready_prompt(delta: str) -> bool:
    """识别 Claude 重新出现的输入提示符。"""
    for line in reversed(delta.splitlines()):
        stripped = normalize_terminal_line(line)
        if not stripped or is_separator_line(stripped):
            continue
        return stripped in {"❯", ">"}
    return False


def is_claude_prompt_cursor(cursor_line: str, cursor_x: int) -> bool:
    """判断光标是否停在 Claude 的空输入提示符处。"""
    normalized = cursor_line.replace("\x00", "").replace("\xa0", " ")
    stripped = normalized.strip()
    if stripped not in {"❯", ">"}:
        return False
    prompt_index = max(normalized.find("❯"), normalized.find(">"))
    return cursor_x > prompt_index


def has_claude_ready_prompt_tail(text: str) -> bool:
    """判断当前屏幕最后一个有效行是否是 Claude 空输入提示符。"""
    for line in reversed(text.splitlines()):
        stripped = normalize_terminal_line(line)
        if not stripped or is_separator_line(stripped):
            continue
        return stripped in {"❯", ">"}
    return False


def clean_claude_delta(delta: str) -> str:
    """移除 Claude TUI 尾部状态行、分隔线和输入提示符。"""
    lines = [line.replace("\x00", " ").replace("\xa0", " ") for line in delta.splitlines()]
    while lines:
        stripped = normalize_terminal_line(lines[-1])
        if not stripped:
            lines.pop()
            continue
        if stripped in {"❯", ">"}:
            lines.pop()
            continue
        if CLAUDE_STATUS_RE.match(stripped):
            lines.pop()
            continue
        if is_separator_line(stripped):
            lines.pop()
            continue
        break

    lines = [line for line in lines if not is_claude_noise_line(line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("⏺ "):
        indent_len = len(lines[0]) - len(lines[0].lstrip())
        lines[0] = " " * indent_len + lines[0].lstrip()[2:].lstrip()
    return "\n".join(lines).strip()


def is_claude_noise_line(line: str) -> bool:
    """识别 Claude TUI 的进度/状态噪声行。"""
    stripped = normalize_terminal_line(line)
    if not stripped:
        return False
    if CLAUDE_STATUS_RE.match(stripped):
        return True
    if CLAUDE_TIP_RE.match(stripped):
        return True
    if CLAUDE_SPINNER_RE.match(stripped):
        return True
    if "⏺" in stripped:
        return False
    if not stripped.endswith("..."):
        return False
    word = stripped[:-3].replace("-", "").replace("'", "").replace(" ", "")
    return word.isalpha()


def has_claude_active_work_after_answer(text: str) -> bool:
    """判断 Claude 回答后是否仍有运行中的工具或活跃状态。"""
    if not text:
        return False
    lines = text.splitlines()
    answer_index = -1
    for index, line in enumerate(lines):
        if normalize_terminal_line(line).startswith("⏺"):
            answer_index = index
    if answer_index < 0:
        return False
    for line in lines[answer_index + 1:]:
        stripped = normalize_terminal_line(line)
        if not stripped:
            continue
        lower = stripped.lower()
        if CLAUDE_SPINNER_RE.match(stripped):
            return True
        if stripped in {"Running…", "Running..."}:
            return True
        if "ctrl+b to run in background" in lower:
            return True
    return False


def looks_like_claude_delta(delta: str) -> bool:
    """判断本轮输出是否像 Claude TUI 内容。"""
    return (
        "⏺" in delta
        or "✻" in delta
        or has_claude_ready_prompt(delta)
    )
