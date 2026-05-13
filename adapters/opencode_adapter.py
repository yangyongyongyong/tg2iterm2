"""OpenCode CLI 交互适配器。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from adapters.base import InteractiveAdapter, SlashCommand


OPENCODE_FOLLOW_UP_RE = re.compile(r"^→\s+Add a follow-up\s*$", re.IGNORECASE)
OPENCODE_STATUS_RE = re.compile(r"^(?:>\s+build\b.*|Composer\b.*Auto-run\b.*)$", re.IGNORECASE)


class OpenCodeAdapter(InteractiveAdapter):
    """OpenCode 交互适配器。"""

    def __init__(self, context_dir: Path | str) -> None:
        self._context_dir = Path(context_dir)
        self._model: str | None = None
        self._variant: str | None = None

    @property
    def context_dir(self) -> Path:
        return self._context_dir

    @context_dir.setter
    def context_dir(self, value: Path | str) -> None:
        self._context_dir = Path(value)

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def cli_command(self) -> str:
        return "opencode"

    @property
    def model(self) -> str | None:
        return self._model

    @model.setter
    def model(self, value: str | None) -> None:
        self._model = value

    @property
    def variant(self) -> str | None:
        return self._variant

    @variant.setter
    def variant(self, value: str | None) -> None:
        self._variant = value

    def get_launch_command(self, session_id: str | None = None) -> str:
        self._context_dir.mkdir(parents=True, exist_ok=True)
        _ = session_id
        parts = ["opencode", "-c", shlex.quote(str(self._context_dir))]
        return " ".join(parts)

    def is_turn_complete(
        self,
        delta: str,
        screen_text: str,
        cursor_line: str,
        cursor_x: int,
    ) -> bool:
        return self.has_answer(delta) and _has_ready_state(screen_text)

    def clean_output(self, delta: str) -> str:
        lines = [line.replace("\x00", " ").replace("\xa0", " ") for line in delta.splitlines()]
        while lines:
            stripped = lines[-1].strip()
            if not stripped or _is_noise_line(stripped):
                lines.pop()
                continue
            break
        lines = [line for line in lines if not _is_noise_line(line.strip())]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines).strip()

    def has_answer(self, delta: str) -> bool:
        return bool(self.clean_output(delta))

    def get_slash_commands(self) -> list[SlashCommand]:
        return []


def _has_ready_state(screen_text: str) -> bool:
    for line in reversed(screen_text.splitlines()):
        stripped = line.replace("\x00", "").replace("\xa0", " ").strip()
        if not stripped:
            continue
        if stripped in {">", "❯", "~"}:
            return True
        if OPENCODE_FOLLOW_UP_RE.match(stripped):
            return True
    return False


def _is_noise_line(stripped: str) -> bool:
    if not stripped:
        return True
    if stripped in {">", "❯", "~", "<system-reminder>", "</system-reminder>", "Auto-run"}:
        return True
    if OPENCODE_FOLLOW_UP_RE.match(stripped):
        return True
    if OPENCODE_STATUS_RE.match(stripped):
        return True
    if set(stripped) <= {"─", "-", "━", "═", "▄", "▀", " ", "▌", "▐", "█"}:
        return True
    return False
