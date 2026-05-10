"""交互式 CLI 适配器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class AdapterResult:
    """适配器单轮交互结果。"""

    output: str
    exit_status: int | None = None


@dataclass
class SlashCommand:
    """CLI 可用的斜杠命令。"""

    name: str
    description: str


class InteractiveAdapter(ABC):
    """交互式 CLI 适配器基类。

    每个 CLI（Claude / Cursor / 未来新增的工具）实现自己的子类，
    提供各自的 TUI 输入输出解析层，同时共用 iTerm2 底层控制能力。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """适配器名称，如 "claude"、"cursor"。"""

    @property
    @abstractmethod
    def cli_command(self) -> str:
        """启动 CLI 的命令，如 "claude"、"agent"。"""

    def get_launch_command(self, session_id: str | None = None) -> str:
        """返回启动 CLI 的完整命令。

        Args:
            session_id: 要恢复的会话 ID。传入则用 --resume，否则启动新会话。
        """
        if session_id:
            return f"{self.cli_command} --resume {session_id}"
        return self.cli_command

    @abstractmethod
    def is_turn_complete(
        self,
        delta: str,
        screen_text: str,
        cursor_line: str,
        cursor_x: int,
    ) -> bool:
        """判断 CLI 当前回合是否已完成，回到可输入状态。

        Args:
            delta: 本轮输入后的新增输出文本。
            screen_text: 当前完整屏幕文本。
            cursor_line: 光标所在行文本。
            cursor_x: 光标列位置。
        """

    @abstractmethod
    def clean_output(self, delta: str) -> str:
        """清理 TUI 噪声（spinner、状态行、分隔线等），提取纯净回答文本。"""

    @abstractmethod
    def has_answer(self, delta: str) -> bool:
        """判断输出中是否已包含 CLI 的回答正文（而非仅 spinner/tip）。"""

    def get_slash_commands(self) -> list[SlashCommand]:
        """返回该 CLI 可用的斜杠命令列表，用于动态生成 Bot 菜单。

        默认返回空列表，子类可覆盖。
        """
        return []

    def get_done_signal_path(self) -> str | None:
        """返回 hook 完成信号文件路径，无信号机制时返回 None。"""
        return None
