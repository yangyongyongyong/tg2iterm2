#!/usr/bin/env python3
"""Claude Code PermissionRequest hook — 桥接到 tg2iterm2 bot。

由 Claude Code 在 PermissionRequest 事件时作为子进程调用。
通过标记文件中的 session_id 绑定 bot 管理的会话，
确保不干扰 IDE 内部或其他终端的 Claude 会话。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks.permission_bridge import (
    write_permission_request,
    poll_permission_response,
)

ACTIVE_MARKER_PATH = Path("/tmp/tg2iterm2_claude_active")
TIMEOUT = 120


def read_active_session_id() -> str | None:
    """从标记文件读取 bot 绑定的 session_id。"""
    try:
        data = json.loads(ACTIVE_MARKER_PATH.read_text())
        return data.get("session_id")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def bind_session_id(session_id: str) -> None:
    """首次匹配时将 session_id 写入标记文件。"""
    try:
        data = {}
        if ACTIVE_MARKER_PATH.exists():
            try:
                data = json.loads(ACTIVE_MARKER_PATH.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        data["session_id"] = session_id
        ACTIVE_MARKER_PATH.write_text(json.dumps(data))
    except OSError:
        pass


def is_bot_session(request_data: dict) -> bool:
    """判断当前 hook 调用是否属于 bot 管理的会话。"""
    if not ACTIVE_MARKER_PATH.exists():
        return False

    session_id = request_data.get("session_id", "")
    bound_id = read_active_session_id()

    if bound_id is None:
        if session_id:
            bind_session_id(session_id)
        return True

    return session_id == bound_id


def main() -> None:
    """读取 Claude 权限请求，转发到 Bot，等待用户响应。"""
    request_data = json.loads(sys.stdin.read())

    if not is_bot_session(request_data):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": "allow",
                "permissionDecisionReason": "非 tg2iterm2 bot 会话，自动放行",
            }
        }))
        return

    request_data["source"] = "claude"
    write_permission_request(request_data)
    response = poll_permission_response(timeout=TIMEOUT)

    if response is not None:
        print(json.dumps(response))
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Telegram 用户未响应，自动拒绝",
            }
        }))


if __name__ == "__main__":
    main()
