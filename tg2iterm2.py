#!/Users/luca/miniforge3/envs/py311/bin/python
"""Telegram 远程操作 iTerm2 的启动入口。"""

from __future__ import annotations

import asyncio

from bot_app import Tg2ITermApp
from config import load_config
from iterm_controller import ITermController
from telegram_client import TelegramBotClient


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
    asyncio.run(main())
