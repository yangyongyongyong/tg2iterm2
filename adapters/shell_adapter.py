"""普通 Shell 命令适配器。

Shell 模式下不需要 TUI 检测或回合完成判断，
直接委托 ITermController 的 run_command_stream 执行命令。
此适配器不继承 InteractiveAdapter（因为 Shell 不是交互式 CLI）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ShellResult:
    """Shell 命令执行结果。"""

    output: str
    exit_status: int | None = None
