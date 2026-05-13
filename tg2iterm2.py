#!/Users/luca/miniforge3/envs/py311/bin/python
"""Telegram 远程操作 iTerm2 的启动入口。"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

from bot_app import Tg2ITermApp
from config import load_config
from iterm_controller import ITermController
from telegram_client import TelegramBotClient


_INSTANCE_LOCK: object | None = None


def _acquire_instance_lock() -> None:
    """确保同一时间只有一个 tg2iterm2 进程在运行。"""
    global _INSTANCE_LOCK
    lock_path = Path.home() / ".tg2iterm2" / "tg2iterm2.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("tg2iterm2 已在运行，请先停止旧进程") from exc
    handle.write(str(os.getpid()))
    handle.flush()
    _INSTANCE_LOCK = handle


async def main() -> None:
    """加载配置并启动 Telegram 到 iTerm2 的桥接服务。"""
    config = load_config()
    telegram = TelegramBotClient(config.bot_token)
    iterm = ITermController(
        default_tab_number=config.default_tab_number,
        claude_done_signal=config.claude_done_signal,
        claude_hook_timeout=config.claude_hook_timeout,
        cursor_done_signal=getattr(config, 'cursor_done_signal', None),
        cursor_hook_timeout=getattr(config, 'cursor_hook_timeout', 300.0),
    )
    app = Tg2ITermApp(config=config, telegram=telegram, iterm=iterm)
    await app.run()


if __name__ == "__main__":
    _acquire_instance_lock()
    asyncio.run(main())
哦