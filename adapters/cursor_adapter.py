"""Cursor CLI 交互适配器。

Cursor CLI (`agent`) 在 iTerm2 中以交互模式运行，
本适配器提供 TUI 输出解析和回合完成检测。

Cursor 的 TUI 格式与 Claude 不同，需要独立的检测逻辑。
初版基于 Cursor CLI 的已知行为编写，可通过集成测试逐步完善。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from adapters.base import InteractiveAdapter, SlashCommand


CURSOR_DONE_SIGNAL_DEFAULT = Path("/tmp/tg2iterm2_cursor_done")

CURSOR_THINKING_RE = re.compile(r"^\s*Thinking\s*\.{3}\s*$", re.IGNORECASE)
CURSOR_TOOL_CALL_RE = re.compile(r"^\s*(?:Running|Executing)\s+.+$", re.IGNORECASE)

SKILLS_DIR = Path.home() / ".cursor" / "skills"


class CursorAdapter(InteractiveAdapter):
    """Cursor CLI 交互适配器。"""

    def __init__(
        self,
        done_signal: str | None = None,
        hook_timeout: float = 300.0,
    ) -> None:
        """初始化 Cursor 适配器。

        Args:
            done_signal: Cursor stop hook 信号文件路径。
            hook_timeout: hook 等待超时时间（秒）。
        """
        self._done_signal = Path(done_signal) if done_signal else CURSOR_DONE_SIGNAL_DEFAULT
        self._hook_timeout = hook_timeout

    @property
    def name(self) -> str:
        """适配器名称。"""
        return "cursor"

    @property
    def cli_command(self) -> str:
        """启动 Cursor CLI 的命令。"""
        return "agent"

    def is_turn_complete(
        self,
        delta: str,
        screen_text: str,
        cursor_line: str,
        cursor_x: int,
    ) -> bool:
        """判断 Cursor CLI 当前回合是否已完成。

        Cursor CLI 回合完成的判据：
        1. delta 中包含回答正文
        2. 屏幕底部回到空输入提示符
        3. 光标在提示符右侧
        4. 无活跃工具执行
        """
        return (
            self.has_answer(delta)
            and not _has_active_work(delta)
            and not _has_active_work(screen_text)
            and _is_cursor_prompt(cursor_line, cursor_x)
            and _has_prompt_tail(screen_text)
        )

    def clean_output(self, delta: str) -> str:
        """清理 Cursor TUI 噪声，提取纯净回答文本。"""
        lines = [
            line.replace("\x00", " ").replace("\xa0", " ")
            for line in delta.splitlines()
        ]
        while lines:
            stripped = _normalize(lines[-1])
            if not stripped:
                lines.pop()
                continue
            if stripped in {">", "❯"}:
                lines.pop()
                continue
            if _is_separator(stripped):
                lines.pop()
                continue
            if _is_noise_line(stripped):
                lines.pop()
                continue
            break

        lines = [line for line in lines if not _is_noise_line(_normalize(line))]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines).strip()

    def has_answer(self, delta: str) -> bool:
        """判断输出中是否包含 Cursor 的回答正文。

        Cursor 的回答不以特定标记开头（不像 Claude 的 ⏺），
        因此只要有非噪声非提示符的实质内容即视为有回答。
        """
        for line in delta.splitlines():
            stripped = _normalize(line)
            if not stripped:
                continue
            if stripped in {">", "❯"}:
                continue
            if _is_separator(stripped):
                continue
            if _is_noise_line(stripped):
                continue
            return True
        return False

    def get_slash_commands(self) -> list[SlashCommand]:
        """扫描 ~/.cursor/skills/ 目录获取可用 skill，按名称排序。"""
        commands: list[SlashCommand] = []
        if not SKILLS_DIR.is_dir():
            return commands
        for skill_dir in SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                desc = _parse_skill_description(skill_md)
                commands.append(SlashCommand(
                    name=skill_dir.name,
                    description=desc or skill_dir.name,
                ))
        commands.sort(key=lambda c: c.name)
        return commands

    def get_done_signal_path(self) -> str | None:
        """返回 Cursor stop hook 信号文件路径。"""
        return str(self._done_signal)

    def read_hook_signal_ns(self) -> int | None:
        """读取 hook 信号文件中的纳秒时间戳，失败返回 None。"""
        try:
            content = self._done_signal.read_text().strip()
            return int(content)
        except (OSError, ValueError):
            return None


def _normalize(line: str) -> str:
    """移除填充字符并裁剪空白。"""
    return line.replace("\x00", "").replace("\xa0", " ").strip()


def _is_separator(stripped: str) -> bool:
    """判断是否为分隔线。"""
    return bool(stripped) and set(stripped) <= {"─", "-", "━", "═"}


def _is_cursor_prompt(cursor_line: str, cursor_x: int) -> bool:
    """判断光标是否在 Cursor 的空输入提示符处。"""
    normalized = cursor_line.replace("\x00", "").replace("\xa0", " ")
    stripped = normalized.strip()
    if stripped not in {">", "❯"}:
        return False
    prompt_index = max(normalized.find(">"), normalized.find("❯"))
    return cursor_x > prompt_index


def _has_prompt_tail(text: str) -> bool:
    """判断屏幕最后一个有效行是否是空输入提示符。"""
    for line in reversed(text.splitlines()):
        stripped = _normalize(line)
        if not stripped or _is_separator(stripped):
            continue
        return stripped in {">", "❯"}
    return False


def _is_noise_line(stripped: str) -> bool:
    """识别 Cursor TUI 的噪声行（spinner、thinking 等）。"""
    if not stripped:
        return False
    if CURSOR_THINKING_RE.match(stripped):
        return True
    if CURSOR_TOOL_CALL_RE.match(stripped):
        return True
    return False


def _has_active_work(text: str) -> bool:
    """判断是否仍有运行中的工具或活跃状态。"""
    if not text:
        return False
    for line in text.splitlines():
        stripped = _normalize(line)
        if not stripped:
            continue
        lower = stripped.lower()
        if stripped in {"Running…", "Running..."}:
            return True
        if "thinking" in lower and lower.endswith("..."):
            return True
    return False


def _parse_skill_description(path: Path) -> str:
    """从 SKILL.md 的 frontmatter 提取 description，支持 YAML 多行格式。"""
    try:
        content = path.read_text(errors="ignore")
    except OSError:
        return ""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return ""
    block = match.group(1)
    lines = block.splitlines()
    for i, line in enumerate(lines):
        kv = re.match(r"^description:\s*(.+)$", line)
        if kv:
            value = kv.group(1).strip()
            if value in (">-", ">", "|", "|-"):
                parts = []
                for next_line in lines[i + 1:]:
                    if next_line.startswith(" ") or next_line.startswith("\t"):
                        parts.append(next_line.strip())
                    else:
                        break
                return " ".join(parts)[:100] if parts else ""
            value = value.strip("'\"")
            return value[:100]
    return ""
