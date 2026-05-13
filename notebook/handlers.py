"""记事本模式的处理方法，供 Tg2ITermApp 使用。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_client import TelegramBotClient
from notebook.manager import NoteManager
from notebook.models import Note, NoteBlock, BlockType


class NoteHandlers:
    """记事本模式的处理方法集合。"""

    def __init__(
        self,
        telegram: TelegramBotClient,
        note_manager: NoteManager,
    ):
        self._telegram = telegram
        self._note_manager = note_manager

    async def send_notebook_menu(self, chat_id: int) -> None:
        """发送记事本主菜单。"""
        from notebook import ui as notebook_ui
        keyboard = notebook_ui.build_main_menu_keyboard()
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(
            chat_id,
            "记事本\n\n请选择操作：",
            reply_markup,
        )

    async def send_note_list(self, chat_id: int, notes: list[Note] | None = None) -> None:
        """发送记事列表。"""
        from notebook import ui as notebook_ui
        if notes is None:
            notes = self._note_manager.get_all_notes(chat_id)
        text = notebook_ui.format_note_list(notes)
        keyboard = notebook_ui.build_note_list_keyboard(notes)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_note_detail(self, chat_id: int, note_id: str) -> None:
        """发送记事详情。"""
        from notebook import ui as notebook_ui
        note = self._note_manager.get_note(note_id)
        if not note:
            await self._telegram.send_message(chat_id, "记事不存在")
            return

        for index, block in enumerate(note.blocks, 1):
            if not block.is_image() or not block.file_path:
                continue
            path = Path(block.file_path)
            if not path.exists():
                continue
            caption = f"图片 {index}: {path.name}"
            try:
                await self._telegram.send_photo(chat_id, str(path), caption=caption)
            except Exception:
                await self._telegram.send_message(chat_id, f"图片文件不存在或发送失败: {path.name}")

        text = notebook_ui.format_note_detail(note)
        keyboard = notebook_ui.build_note_detail_keyboard(note)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_create_prompt(self, chat_id: int) -> None:
        """发送创建记事的提示。"""
        await self._telegram.send_message(
            chat_id,
            "请开始输入笔记内容，支持以下格式：\n"
            "- 文本：直接输入文字\n"
            "- 图片：发送图片\n"
            "- 语音：发送语音消息\n\n"
            "可以连续输入多条，完成后点击「结束编辑」按钮保存。\n"
            "发送 /exit 退出记事本模式",
        )

    async def handle_create_input(self, chat_id: int, text: str) -> bool:
        """处理创建记事的输入。

        Returns:
            是否成功创建
        """
        from notebook import ui as notebook_ui
        content, tags = notebook_ui.parse_tags(text)
        if not content.strip():
            await self._telegram.send_message(chat_id, "记事内容不能为空")
            return False

        note = self._note_manager.add_note(
            chat_id=chat_id,
            blocks=[NoteBlock(type=BlockType.TEXT, content=content)],
            tags=tags,
        )
        tag_text = note.get_tag_text()
        await self._telegram.send_message(
            chat_id,
            f"✅ 记事已创建\n\n"
            f"📌 {note.get_summary()}\n"
            f"{'标签: ' + tag_text if tag_text else ''}\n"
            f"ID: {note.id}",
        )
        return True

    async def handle_voice_note(self, chat_id: int, voice_file_path: str, transcript: str, duration: int) -> bool:
        """处理语音记事。

        Args:
            chat_id: Telegram Chat ID
            voice_file_path: 语音文件本地路径
            transcript: 语音转写文本
            duration: 语音时长（秒）

        Returns:
            是否成功创建
        """
        note = self._note_manager.add_note(
            chat_id=chat_id,
            blocks=[
                NoteBlock(
                    type=BlockType.VOICE,
                    content=transcript,
                    file_path=voice_file_path,
                    duration=duration,
                )
            ],
        )
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration >= 60 else f"{duration}s"
        await self._telegram.send_message(
            chat_id,
            f"✅ 语音记事已创建\n\n"
            f"🎤 [{duration_str}] {transcript[:100]}{'...' if len(transcript) > 100 else ''}\n"
            f"ID: {note.id}",
        )
        return True

    async def handle_delete(self, chat_id: int, note_id: str) -> bool:
        """删除记事。"""
        success = self._note_manager.delete_note(note_id)
        if success:
            await self._telegram.send_message(chat_id, "记事已删除")
        else:
            await self._telegram.send_message(chat_id, "删除失败")
        return success

    async def send_delete_confirm(self, chat_id: int, note_id: str) -> None:
        """发送删除确认。"""
        from notebook import ui as notebook_ui
        note = self._note_manager.get_note(note_id)
        if not note:
            await self._telegram.send_message(chat_id, "记事不存在")
            return
        keyboard = notebook_ui.build_delete_confirm_keyboard(note_id)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(
            chat_id,
            f"确认删除记事？\n\n内容：{note.get_summary()}",
            reply_markup,
        )

    async def send_search_prompt(self, chat_id: int) -> None:
        """发送搜索提示。"""
        await self._telegram.send_message(
            chat_id,
            "请输入搜索关键词：\n\n"
            "支持以下格式：\n"
            "- 普通关键词：搜索内容包含关键词的记事\n"
            "- #标签：按标签搜索（如 #工作）\n"
            "- 日期范围：2026-01-01 2026-05-01\n"
            "- 组合：关键词 #标签 2026-01-01 2026-05-01",
        )

    async def handle_search_input(self, chat_id: int, text: str) -> None:
        """处理搜索输入。"""
        from notebook import ui as notebook_ui
        keyword, tags, start_date, end_date = notebook_ui.parse_search_query(text)

        notes = self._note_manager.search_notes(
            chat_id=chat_id,
            keyword=keyword,
            tags=tags if tags else None,
            start_date=start_date,
            end_date=end_date,
        )

        if not notes:
            await self._telegram.send_message(chat_id, "未找到匹配的记事")
            return

        await self.send_note_list(chat_id, notes)

    async def send_date_filter_prompt(self, chat_id: int) -> None:
        """发送日期过滤提示。"""
        await self._telegram.send_message(
            chat_id,
            "请输入日期范围（格式：YYYY-MM-DD YYYY-MM-DD）：\n\n"
            "例如：2026-01-01 2026-05-01\n"
            "发送 /exit 退出记事本模式",
        )

    async def handle_date_filter_input(self, chat_id: int, text: str) -> None:
        """处理日期过滤输入。"""
        from notebook import ui as notebook_ui
        try:
            start_date, end_date = notebook_ui.parse_date_range(text)
            notes = self._note_manager.get_notes_by_date_range(chat_id, start_date, end_date)
            if not notes:
                await self._telegram.send_message(chat_id, "该日期范围内没有记事")
                return
            await self.send_note_list(chat_id, notes)
        except ValueError as exc:
            await self._telegram.send_message(chat_id, f"日期格式错误: {exc}")

    async def send_tag_list(self, chat_id: int) -> None:
        """发送标签列表。"""
        tags = self._note_manager.get_all_tags(chat_id)
        if not tags:
            await self._telegram.send_message(chat_id, "暂无标签")
            return
        text = "标签列表：\n\n" + "\n".join(f"#{tag}" for tag in tags)
        await self._telegram.send_message(chat_id, text)
