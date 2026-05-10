"""Telegram Bot API 的轻量异步客户端。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import aiohttp
from telegram_format import md_to_telegram_entities


class TelegramBotClient:
    """封装 Telegram Bot API 的常用请求。"""

    def __init__(self, bot_token: str) -> None:
        """初始化 Telegram 客户端。"""
        self._bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "TelegramBotClient":
        """进入异步上下文并创建 HTTP 会话。"""
        await self.open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        """退出异步上下文并关闭 HTTP 会话。"""
        await self.close()

    async def open(self) -> None:
        """创建可复用的 aiohttp 会话。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=90)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        """关闭 aiohttp 会话。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, payload: dict[str, Any]) -> Any:
        """调用 Telegram Bot API 并返回 result 字段，自动处理 429 限流。"""
        await self.open()
        assert self._session is not None
        url = f"{self._base_url}/{method}"
        async with self._session.post(url, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 30)
                print(f"Telegram 限流，等待 {retry_after}s 后重试")
                await asyncio.sleep(retry_after)
                raise RuntimeError(f"Telegram API {method} 限流: retry_after={retry_after}")
            if not data.get("ok"):
                description = data.get("description", "未知错误")
                raise RuntimeError(f"Telegram API {method} 失败: {description}")
            return data.get("result")

    async def delete_webhook(self, drop_pending_updates: bool = True) -> None:
        """删除 webhook，确保长轮询可用。"""
        await self._request(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
        )

    async def get_updates(
        self,
        offset: int | None,
        timeout: int = 50,
    ) -> list[dict[str, Any]]:
        """长轮询获取 Telegram 更新。"""
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._request("getUpdates", payload)

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> dict[str, Any]:
        """发送文本消息，HTML 解析失败时降级为纯文本。"""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": limit_telegram_text(text),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            return await self._request("sendMessage", payload)
        except RuntimeError as exc:
            if parse_mode and is_telegram_parse_error(str(exc)):
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", limit_telegram_text(text))
                return await self._request("sendMessage", payload)
            raise

    async def send_markdown_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """发送文本，优先 entities 富文本；失败或不可用则纯文本分片发送。"""
        chunks = md_to_telegram_entities(text)
        if not chunks:
            return await self._send_plain_parts(chat_id, text)

        first_result: dict[str, Any] | None = None
        for chunk in chunks:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk.text,
                "disable_web_page_preview": True,
            }
            if chunk.entities:
                payload["entities"] = chunk.entities
            try:
                result = await self._request("sendMessage", payload)
            except RuntimeError:
                result = await self.send_message(chat_id, chunk.text)
            if first_result is None:
                first_result = result
        assert first_result is not None
        return first_result

    async def _send_plain_parts(self, chat_id: int, text: str) -> dict[str, Any]:
        """将文本按行边界分片，以纯文本发送（无 parse_mode）。"""
        parts = _split_text(text, 4000)
        first_result: dict[str, Any] | None = None
        for part in parts:
            result = await self.send_message(chat_id, part)
            if first_result is None:
                first_result = result
        assert first_result is not None
        return first_result

    async def send_message_with_reply_markup(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        """发送带 InlineKeyboard 的消息。"""
        return await self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": limit_telegram_text(text),
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """编辑消息的 reply_markup（移除或替换按钮）。"""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        try:
            await self._request("editMessageReplyMarkup", payload)
        except RuntimeError:
            pass

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """应答 callback_query。"""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        await self._request("answerCallbackQuery", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        """编辑已有消息，HTML 解析失败时降级为纯文本。"""
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": limit_telegram_text(text),
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            await self._request("editMessageText", payload)
        except RuntimeError as exc:
            if "message is not modified" in str(exc):
                return
            if parse_mode and is_telegram_parse_error(str(exc)):
                payload.pop("parse_mode", None)
                stripped = re.sub(r"<[^>]+>", "", limit_telegram_text(text))
                payload["text"] = stripped
                try:
                    await self._request("editMessageText", payload)
                except RuntimeError as inner_exc:
                    if "message is not modified" not in str(inner_exc):
                        raise
            else:
                raise

    async def edit_markdown_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        """编辑消息，优先 entities；失败或不可用则纯文本。"""
        display_text = text if len(text) <= 4000 else text[-4000:]

        chunks = md_to_telegram_entities(display_text)
        if chunks and len(chunks) == 1:
            chunk = chunks[0]
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": chunk.text,
                "disable_web_page_preview": True,
            }
            if chunk.entities:
                payload["entities"] = chunk.entities
            try:
                await self._request("editMessageText", payload)
                return
            except RuntimeError as exc:
                if "message is not modified" in str(exc):
                    return
                if not is_telegram_parse_error(str(exc)):
                    raise

        # entities 不可用或失败，纯文本编辑
        await self.edit_message_text(chat_id, message_id, display_text)

    async def set_my_commands(self, extra_commands: list[dict[str, str]] | None = None) -> None:
        """注册 Telegram 机器人命令菜单。"""
        commands = [
            {"command": "start", "description": "显示帮助"},
            {"command": "help", "description": "显示帮助"},
            {"command": "tabs", "description": "列出 iTerm2 tab"},
            {"command": "use_tab", "description": "切换默认 tab"},
            {"command": "new_tab", "description": "新建 tab"},
            {"command": "send", "description": "只输入文本不回车"},
            {"command": "enter", "description": "只发送回车键"},
            {"command": "ctrl_c", "description": "发送 Ctrl+C"},
            {"command": "ctrl_d", "description": "发送 Ctrl+D"},
            {"command": "last", "description": "获取倒数 N 行"},
            {"command": "get_last_10_line", "description": "获取倒数 10 行"},
            {"command": "fetch_file_or_dir", "description": "发送文件/图片（目录浏览）"},
            {"command": "send_2_server", "description": "发送文件到服务端"},
            {"command": "stop_receive", "description": "停止接收文件"},
        ]
        if extra_commands:
            commands.extend(extra_commands)
        await self._request("setMyCommands", {"commands": commands})

    async def download_file_by_id(
        self,
        file_id: str,
        directory: Path,
        filename_prefix: str,
        default_suffix: str = ".jpg",
    ) -> Path:
        """按 Telegram file_id 下载文件到本地目录。"""
        file_info = await self._request("getFile", {"file_id": file_id})
        file_path = str(file_info["file_path"])
        suffix = Path(file_path).suffix or default_suffix
        safe_prefix = sanitize_filename(filename_prefix)
        destination = directory / f"{safe_prefix}{suffix}"
        directory.mkdir(parents=True, exist_ok=True)

        await self.open()
        assert self._session is not None
        url = f"{self._file_base_url}/{file_path}"
        async with self._session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"下载 Telegram 文件失败: HTTP {response.status}")
            data = await response.read()
        destination.write_bytes(data)
        return destination

    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """发送本地图片文件到 Telegram 聊天。"""
        await self.open()
        assert self._session is not None
        url = f"{self._base_url}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        data.add_field(
            "photo",
            open(photo_path, "rb"),
            filename=Path(photo_path).name,
        )
        if caption:
            data.add_field("caption", caption[:1024])
        async with self._session.post(url, data=data) as response:
            body = await response.json()
        if not body.get("ok"):
            raise RuntimeError(f"sendPhoto 失败: {body.get('description', body)}")
        return body["result"]

    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """发送本地文件到 Telegram 聊天（sendDocument API）。"""
        await self.open()
        assert self._session is not None
        url = f"{self._base_url}/sendDocument"
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        data.add_field(
            "document",
            open(file_path, "rb"),
            filename=Path(file_path).name,
        )
        if caption:
            data.add_field("caption", caption[:1024])
        async with self._session.post(url, data=data) as response:
            body = await response.json()
        if not body.get("ok"):
            raise RuntimeError(f"sendDocument 失败: {body.get('description', body)}")
        return body["result"]


def _split_text(text: str, max_len: int = 4000) -> list[str]:
    """按行边界将文本分成不超过 max_len 的片段，确保不截断内容。"""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current = []
            current_len = 0
        if len(line) > max_len:
            if current:
                parts.append("\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), max_len):
                parts.append(line[i:i + max_len])
        else:
            current.append(line)
            current_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts or [text]


def limit_telegram_text(text: str, limit: int = 4000) -> str:
    """将文本限制在 Telegram 单条消息长度内，确保 HTML 标签闭合。"""
    if len(text) <= limit:
        return text
    marker = "\n...[前面内容已截断]...\n"
    truncated = marker + text[-(limit - len(marker)):]
    if "</pre>" in truncated and "<pre>" not in truncated:
        truncated = "<pre>" + truncated
    return truncated


def is_telegram_parse_error(description: str) -> bool:
    """判断 Telegram 错误是否属于富文本解析失败。"""
    lower = description.lower()
    return (
        "can't parse entities" in lower
        or "can't find end of" in lower
        or "parse entities" in lower
    )


def sanitize_filename(value: str) -> str:
    """清理文件名，避免生成包含特殊字符的临时文件路径。"""
    safe_chars = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            safe_chars.append(char)
        else:
            safe_chars.append("_")
    safe_value = "".join(safe_chars).strip("._")
    return safe_value or "telegram_file"
