"""交互式 CLI 适配器层。"""

from adapters.claude_adapter import ClaudeAdapter
from adapters.cursor_adapter import CursorAdapter
from adapters.opencode_adapter import OpenCodeAdapter

__all__ = ["ClaudeAdapter", "CursorAdapter", "OpenCodeAdapter"]
