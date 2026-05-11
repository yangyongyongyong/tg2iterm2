"""提醒模式的处理方法，供 Tg2ITermApp 使用。"""

from __future__ import annotations

import asyncio
from typing import Any

from telegram_client import TelegramBotClient
from reminder.manager import ReminderManager
from reminder.models import Reminder
from reminder.parser import ReminderParser
from reminder import ui as reminder_ui


class ReminderHandlers:
    """提醒模式的处理方法集合。"""

    def __init__(
        self,
        telegram: TelegramBotClient,
        reminder_manager: ReminderManager,
        reminder_parser: ReminderParser,
    ):
        self._telegram = telegram
        self._reminder_manager = reminder_manager
        self._reminder_parser = reminder_parser

    async def send_reminder_menu(self, chat_id: int) -> None:
        """发送提醒主菜单。"""
        keyboard = reminder_ui.build_main_menu_keyboard()
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(
            chat_id,
            "提醒管理\n\n请选择操作：",
            reply_markup,
        )

    async def send_reminder_list(self, chat_id: int) -> None:
        """发送提醒列表。"""
        reminders = self._reminder_manager.get_all_reminders(chat_id)
        next_times_map = {
            r.id: self._reminder_manager.get_next_fire_times(r.id, count=3)
            for r in reminders
        }
        text = reminder_ui.format_reminder_list(reminders, next_times_map)
        keyboard = reminder_ui.build_reminder_list_keyboard(reminders, next_times_map)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_completed_list(self, chat_id: int) -> None:
        """发送已完成提醒列表。"""
        reminders = self._reminder_manager.get_completed_reminders(chat_id)
        text = reminder_ui.format_completed_reminders(reminders)
        keyboard = reminder_ui.build_completed_list_keyboard(reminders)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_completed_detail(self, chat_id: int, reminder_id: str) -> None:
        """发送已完成提醒详情。"""
        reminder = self._reminder_manager.get_reminder(reminder_id)
        if not reminder:
            await self._telegram.send_message(chat_id, "提醒不存在")
            return
        text = reminder_ui.format_reminder_detail(reminder)
        keyboard = reminder_ui.build_completed_detail_keyboard(reminder)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_reminder_detail(self, chat_id: int, reminder_id: str) -> None:
        """发送提醒详情。"""
        reminder = self._reminder_manager.get_reminder(reminder_id)
        if not reminder:
            await self._telegram.send_message(chat_id, "提醒不存在")
            return
        next_times = self._reminder_manager.get_next_fire_times(reminder_id, count=3)
        text = reminder_ui.format_reminder_detail(reminder, next_times)
        keyboard = reminder_ui.build_reminder_detail_keyboard(reminder)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(chat_id, text, reply_markup)

    async def send_create_prompt(self, chat_id: int) -> None:
        """发送创建提醒的提示。"""
        await self._telegram.send_message(
            chat_id,
            "请输入提醒内容，例如：\n"
            "- 每周三晚上8点提醒我自驾游\n"
            "- 每天22点提醒我跑步\n"
            "- 2026-05-15 10:00 提醒我开会\n\n"
            "发送 /exit 退出提醒模式",
        )

    async def handle_create_input(self, chat_id: int, text: str) -> bool:
        """处理创建提醒的输入。

        Returns:
            是否成功创建
        """
        result = await self._reminder_parser.parse(text)
        if not result:
            await self._telegram.send_message(
                chat_id,
                "无法解析提醒内容，请尝试更明确的格式，例如：\n"
                "- 每周三 20:00 提醒我...\n"
                "- 每天 22:00 提醒我...",
            )
            return False

        try:
            reminder = await self._reminder_manager.add_reminder(
                chat_id=chat_id,
                content=result["content"],
                trigger_type=result["trigger_type"],
                trigger_config=result["trigger_config"],
            )
            schedule = reminder.get_human_readable_schedule()
            await self._telegram.send_message(
                chat_id,
                f"已创建提醒\n"
                f"内容：{reminder.content}\n"
                f"时间：{schedule}\n"
                f"ID：{reminder.id}",
            )
            return True
        except Exception as exc:
            await self._telegram.send_message(chat_id, f"创建提醒失败: {exc}")
            return False

    async def handle_pause(self, chat_id: int, reminder_id: str) -> bool:
        """暂停提醒。"""
        success = await self._reminder_manager.pause_reminder(reminder_id)
        if success:
            await self._telegram.send_message(chat_id, "提醒已暂停")
        else:
            await self._telegram.send_message(chat_id, "暂停失败")
        return success

    async def handle_resume(self, chat_id: int, reminder_id: str) -> bool:
        """恢复提醒。"""
        success = await self._reminder_manager.resume_reminder(reminder_id)
        if success:
            await self._telegram.send_message(chat_id, "提醒已恢复")
        else:
            await self._telegram.send_message(chat_id, "恢复失败")
        return success

    async def handle_delete(self, chat_id: int, reminder_id: str) -> bool:
        """删除提醒。"""
        success = await self._reminder_manager.remove_reminder(reminder_id)
        if success:
            await self._telegram.send_message(chat_id, "提醒已删除")
        else:
            await self._telegram.send_message(chat_id, "删除失败")
        return success

    async def send_delete_confirm(self, chat_id: int, reminder_id: str) -> None:
        """发送删除确认。"""
        reminder = self._reminder_manager.get_reminder(reminder_id)
        if not reminder:
            await self._telegram.send_message(chat_id, "提醒不存在")
            return
        keyboard = reminder_ui.build_delete_confirm_keyboard(reminder_id)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(
            chat_id,
            f"确认删除提醒？\n\n内容：{reminder.content}",
            reply_markup,
        )

    async def send_edit_menu(self, chat_id: int, reminder_id: str) -> None:
        """发送编辑菜单。"""
        reminder = self._reminder_manager.get_reminder(reminder_id)
        if not reminder:
            await self._telegram.send_message(chat_id, "提醒不存在")
            return
        keyboard = reminder_ui.build_edit_keyboard(reminder)
        reply_markup = {"inline_keyboard": keyboard}
        await self._telegram.send_message_with_reply_markup(
            chat_id,
            f"编辑提醒：{reminder.content}\n\n请选择要修改的内容：",
            reply_markup,
        )

    async def handle_edit_content(self, chat_id: int, reminder_id: str, new_content: str) -> bool:
        """修改提醒内容。"""
        reminder = await self._reminder_manager.update_reminder(reminder_id, content=new_content)
        if reminder:
            await self._telegram.send_message(chat_id, f"内容已更新：{reminder.content}")
            return True
        await self._telegram.send_message(chat_id, "更新失败")
        return False

    async def handle_edit_time(self, chat_id: int, reminder_id: str, text: str) -> bool:
        """修改提醒时间。"""
        result = await self._reminder_parser.parse(f"{text} 提醒我")
        if not result:
            await self._telegram.send_message(chat_id, "无法解析时间格式")
            return False

        reminder = await self._reminder_manager.update_reminder(
            reminder_id,
            trigger_type=result["trigger_type"],
            trigger_config=result["trigger_config"],
        )
        if reminder:
            schedule = reminder.get_human_readable_schedule()
            await self._telegram.send_message(chat_id, f"时间已更新：{schedule}")
            return True
        await self._telegram.send_message(chat_id, "更新失败")
        return False

    async def handle_add_info(self, chat_id: int, reminder_id: str, info: str) -> bool:
        """为提醒添加备注信息。"""
        success = await self._reminder_manager.update_reminder_info(reminder_id, info)
        if success:
            reminder = self._reminder_manager.get_reminder(reminder_id)
            await self._telegram.send_message(
                chat_id,
                f"✅ 备注已添加\n\n📌 {reminder.content}\n📝 {info}",
            )
        else:
            await self._telegram.send_message(chat_id, "添加备注失败")
        return success

    async def on_reminder_triggered(self, reminder: Reminder) -> None:
        """提醒触发时的回调。"""
        print(f"[Reminder] 触发提醒: {reminder.id} - {reminder.content} -> chat_id: {reminder.chat_id}")
        text = f"⏰ 提醒：{reminder.content}"
        if reminder.info:
            text += f"\n\n📝 备注：{reminder.info}"
        await self._telegram.send_message(reminder.chat_id, text)
        print(f"[Reminder] 消息已发送")