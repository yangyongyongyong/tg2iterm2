"""tg2iterm2 的环境变量配置。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """保存运行时配置。"""

    bot_token: str
    allowed_chat_id: int
    default_tab_number: int | None
    stream_interval: float
    claude_done_signal: str
    claude_hook_timeout: float
    perm_request_path: str
    perm_response_path: str
    perm_poll_interval: float


def _read_required_env(name: str) -> str:
    """读取必填环境变量，缺失时给出明确错误。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必填环境变量: {name}")
    return value


def _read_float_env(name: str, default: float) -> float:
    """读取浮点型环境变量，格式非法时回退到默认值。"""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _read_int_env(name: str) -> int:
    """读取整数环境变量，格式非法时给出明确错误。"""
    raw_value = _read_required_env(name)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc


def _read_optional_int_env(*names: str) -> int | None:
    """按顺序读取可选整数环境变量。"""
    for name in names:
        raw_value = os.environ.get(name, "").strip()
        if not raw_value:
            continue
        try:
            return int(raw_value)
        except ValueError as exc:
            raise RuntimeError(f"环境变量 {name} 必须是整数") from exc
    return None


def load_config() -> AppConfig:
    """从环境变量加载应用配置。"""
    if sys.platform != "darwin":
        raise RuntimeError("tg2iterm2 仅支持 macOS")

    return AppConfig(
        bot_token=_read_required_env("TG_BOT_TOKEN"),
        allowed_chat_id=_read_int_env("TG_ALLOWED_CHAT_ID"),
        default_tab_number=_read_optional_int_env(
            "TG_DEFAULT_TAB_NUMBER",
            "TG_DEFAULT_TAB_INDEX",
        ),
        stream_interval=_read_float_env("TG_STREAM_INTERVAL", 15.0),
        claude_done_signal=os.environ.get(
            "TG_CLAUDE_DONE_SIGNAL", "/tmp/tg2iterm2_claude_done"
        ).strip() or "/tmp/tg2iterm2_claude_done",
        claude_hook_timeout=_read_float_env("TG_CLAUDE_HOOK_TIMEOUT", 300.0),
        perm_request_path=os.environ.get(
            "TG_PERM_REQUEST_PATH", "/tmp/tg2iterm2_perm_request.json"
        ).strip() or "/tmp/tg2iterm2_perm_request.json",
        perm_response_path=os.environ.get(
            "TG_PERM_RESPONSE_PATH", "/tmp/tg2iterm2_perm_response.json"
        ).strip() or "/tmp/tg2iterm2_perm_response.json",
        perm_poll_interval=_read_float_env("TG_PERM_POLL_INTERVAL", 0.5),
    )
