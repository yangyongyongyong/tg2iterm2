"""通用权限弹窗桥接层。

Claude 和 Cursor 的 hook 脚本通过文件 IPC 与 TG Bot 通信：
  hook 脚本写入请求文件 → Bot 监控并弹出 InlineKeyboard → 用户点击 → Bot 写入响应文件 → hook 读取返回

此模块提供双方共用的文件路径约定和数据格式。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_REQUEST_PATH = Path("/tmp/tg2iterm2_perm_request.json")
DEFAULT_RESPONSE_PATH = Path("/tmp/tg2iterm2_perm_response.json")
DEFAULT_TIMEOUT = 120


def write_permission_request(
    request_data: dict,
    request_path: Path = DEFAULT_REQUEST_PATH,
    response_path: Path = DEFAULT_RESPONSE_PATH,
) -> None:
    """hook 脚本调用：写入权限请求文件并清除旧响应。"""
    response_path.unlink(missing_ok=True)
    request_path.write_text(json.dumps(request_data, ensure_ascii=False))


def poll_permission_response(
    request_path: Path = DEFAULT_REQUEST_PATH,
    response_path: Path = DEFAULT_RESPONSE_PATH,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict | None:
    """hook 脚本调用：轮询等待 Bot 写入的响应，超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if response_path.exists():
            try:
                response = json.loads(response_path.read_text())
            except (json.JSONDecodeError, OSError):
                time.sleep(0.3)
                continue
            response_path.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)
            return response
        time.sleep(0.3)
    request_path.unlink(missing_ok=True)
    return None


def write_permission_response(
    response_data: dict,
    response_path: Path = DEFAULT_RESPONSE_PATH,
) -> None:
    """Bot 侧调用：将用户选择写入响应文件。"""
    response_path.write_text(json.dumps(response_data))
