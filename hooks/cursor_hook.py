#!/usr/bin/env python3
"""Cursor preToolUse / stop hook — 桥接到 tg2iterm2 bot。

由 Cursor 在 preToolUse 或 stop 事件时作为子进程调用。
通过标记文件中的 conversation_id 绑定 bot 管理的会话，
确保不干扰 Cursor IDE 内部或其他终端的 agent 会话。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks.permission_bridge import (
    write_permission_request,
    poll_permission_response,
)

DONE_SIGNAL_PATH = Path("/tmp/tg2iterm2_cursor_done")
ACTIVE_MARKER_PATH = Path("/tmp/tg2iterm2_cursor_active")
TIMEOUT = 120

READONLY_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}


def read_active_conversation_id() -> str | None:
    """从标记文件读取 bot 绑定的 conversation_id。"""
    try:
        data = json.loads(ACTIVE_MARKER_PATH.read_text())
        return data.get("conversation_id")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def bind_conversation_id(conversation_id: str) -> None:
    """首次匹配时将 conversation_id 写入标记文件。"""
    try:
        data = {}
        if ACTIVE_MARKER_PATH.exists():
            try:
                data = json.loads(ACTIVE_MARKER_PATH.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        data["conversation_id"] = conversation_id
        ACTIVE_MARKER_PATH.write_text(json.dumps(data))
    except OSError:
        pass


def is_bot_session(payload: dict) -> bool:
    """判断当前 hook 调用是否属于 bot 管理的会话。

    标记文件不存在 → bot 未激活，放行。
    标记文件存在但无 conversation_id → bot 刚启动，首个会话自动绑定。
    标记文件有 conversation_id → 只处理匹配的会话。
    """
    if not ACTIVE_MARKER_PATH.exists():
        return False

    conv_id = payload.get("conversation_id", "")
    bound_id = read_active_conversation_id()

    if bound_id is None:
        if conv_id:
            bind_conversation_id(conv_id)
        return True

    return conv_id == bound_id


def main() -> None:
    """根据 hook 事件类型分发处理。"""
    payload = json.loads(sys.stdin.read())
    event = payload.get("hook_event_name", "")

    if not is_bot_session(payload):
        if event == "preToolUse":
            print(json.dumps({"permission": "allow"}))
        else:
            print(json.dumps({}))
        return

    if event == "stop":
        handle_stop(payload)
    elif event == "preToolUse":
        handle_pre_tool_use(payload)
    else:
        print(json.dumps({"permission": "allow"}))


def handle_stop(payload: dict) -> None:
    """stop hook: 写入纳秒时间戳信号文件。"""
    DONE_SIGNAL_PATH.write_text(str(time.time_ns()))
    print(json.dumps({}))


def handle_pre_tool_use(payload: dict) -> None:
    """preToolUse hook: 只读工具放行，其他转发到 TG Bot。"""
    tool_name = payload.get("tool_name", "")

    if tool_name in READONLY_TOOLS:
        print(json.dumps({"permission": "allow"}))
        return

    request_data = {
        "source": "cursor",
        "hook_event": "preToolUse",
        "tool_name": tool_name,
        "tool_input": payload.get("tool_input", {}),
        "description": payload.get("agent_message", ""),
        "conversation_id": payload.get("conversation_id", ""),
    }
    write_permission_request(request_data)
    response = poll_permission_response(timeout=TIMEOUT)

    if response is not None:
        print(json.dumps(response))
    else:
        print(json.dumps({
            "permission": "deny",
            "user_message": "Telegram 用户未响应，自动拒绝",
        }))


if __name__ == "__main__":
    main()
