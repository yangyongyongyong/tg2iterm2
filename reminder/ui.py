"""提醒模块的 InlineKeyboard UI 生成。"""

from __future__ import annotations

from typing import Any

from reminder.models import Reminder


def build_main_menu_keyboard() -> list[list[dict[str, Any]]]:
    """
    构建提醒主菜单键盘。

    Returns:
        InlineKeyboard 按钮列表
    """
    return [
        [{"text": "创建提醒", "callback_data": "reminder_create"}],
        [{"text": "查看提醒列表", "callback_data": "reminder_list"}],
        [{"text": "📜 历史记录", "callback_data": "reminder_completed"}],
        [{"text": "退出", "callback_data": "reminder_exit"}],
    ]


def build_reminder_list_keyboard(reminders: list[Reminder]) -> list[list[dict[str, Any]]]:
    """
    构建提醒列表键盘（只显示有效的提醒）。

    Args:
        reminders: 提醒列表

    Returns:
        InlineKeyboard 按钮列表
    """
    keyboard: list[list[dict[str, Any]]] = []
    
    # 过滤出有效的提醒
    active_reminders = [r for r in reminders if r.is_active()]

    for reminder in active_reminders[:8]:  # 最多显示 8 个
        schedule = reminder.get_human_readable_schedule()
        text = f"{reminder.content[:20]} ({schedule})"
        keyboard.append([
            {"text": text, "callback_data": f"reminder_detail_{reminder.id}"},
            {"text": "🗑️", "callback_data": f"reminder_delete_{reminder.id}"},
        ])

    keyboard.append([{"text": "返回", "callback_data": "reminder_menu"}])

    return keyboard


def build_reminder_detail_keyboard(reminder: Reminder) -> list[list[dict[str, Any]]]:
    """
    构建提醒详情键盘。

    Args:
        reminder: 提醒对象

    Returns:
        InlineKeyboard 按钮列表
    """
    keyboard: list[list[dict[str, Any]]] = []

    # 第一行：暂停/恢复、编辑
    if reminder.paused:
        keyboard.append([
            {"text": "▶️ 恢复", "callback_data": f"reminder_resume_{reminder.id}"},
            {"text": "✏️ 编辑", "callback_data": f"reminder_edit_{reminder.id}"},
        ])
    else:
        keyboard.append([
            {"text": "⏸️ 暂停", "callback_data": f"reminder_pause_{reminder.id}"},
            {"text": "✏️ 编辑", "callback_data": f"reminder_edit_{reminder.id}"},
        ])

    # 第二行：删除
    keyboard.append([
        {"text": "🗑️ 删除", "callback_data": f"reminder_delete_{reminder.id}"}
    ])

    # 第三行：返回
    keyboard.append([{"text": "返回列表", "callback_data": "reminder_list"}])

    return keyboard


def build_edit_keyboard(reminder: Reminder) -> list[list[dict[str, Any]]]:
    """
    构建编辑模式键盘。

    Args:
        reminder: 提醒对象

    Returns:
        InlineKeyboard 按钮列表
    """
    return [
        [{"text": "修改内容", "callback_data": f"reminder_edit_content_{reminder.id}"}],
        [{"text": "修改时间", "callback_data": f"reminder_edit_time_{reminder.id}"}],
        [{"text": "取消", "callback_data": f"reminder_detail_{reminder.id}"}],
    ]


def build_delete_confirm_keyboard(reminder_id: str) -> list[list[dict[str, Any]]]:
    """
    构建删除确认键盘。

    Args:
        reminder_id: 提醒 ID

    Returns:
        InlineKeyboard 按钮列表
    """
    return [
        [
            {"text": "确认删除", "callback_data": f"reminder_delete_confirm_{reminder_id}"},
            {"text": "取消", "callback_data": f"reminder_detail_{reminder_id}"},
        ]
    ]


def format_reminder_detail(reminder: Reminder) -> str:
    """
    格式化提醒详情文本。

    Args:
        reminder: 提醒对象

    Returns:
        格式化的文本
    """
    status = "⏸️ 已暂停" if reminder.paused else "✅ 运行中"
    schedule = reminder.get_human_readable_schedule()
    created = reminder.created_at.strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📌 **{reminder.content}**",
        f"",
        f"状态：{status}",
        f"时间：{schedule}",
        f"创建：{created}",
    ]

    if reminder.next_fire_time:
        next_time = reminder.next_fire_time.strftime("%Y-%m-%d %H:%M")
        lines.append(f"下次提醒：{next_time}")

    if reminder.info:
        lines.append(f"")
        lines.append(f"📝 备注：{reminder.info}")

    return "\n".join(lines)


def format_reminder_list(reminders: list[Reminder]) -> str:
    """
    格式化提醒列表文本（只显示有效的提醒）。

    Args:
        reminders: 提醒列表

    Returns:
        格式化的文本
    """
    # 过滤出有效的提醒
    active_reminders = [r for r in reminders if r.is_active()]
    
    if not active_reminders:
        return "📋 暂无待提醒\n\n点击「创建提醒」添加新提醒。"

    lines = ["📋 **待提醒列表**\n"]

    for i, reminder in enumerate(active_reminders[:8], 1):
        schedule = reminder.get_human_readable_schedule()
        lines.append(f"{i}. {reminder.content}")
        lines.append(f"   {schedule}")

    if len(active_reminders) > 8:
        lines.append(f"\n... 还有 {len(active_reminders) - 8} 个提醒")

    return "\n".join(lines)


def build_completed_list_keyboard(reminders: list[Reminder]) -> list[list[dict[str, Any]]]:
    """
    构建已完成提醒列表键盘。

    Args:
        reminders: 已完成提醒列表

    Returns:
        InlineKeyboard 按钮列表
    """
    keyboard: list[list[dict[str, Any]]] = []

    for reminder in reminders[:8]:
        status = "✅" if reminder.triggered else "⏰"
        text = f"{status} {reminder.content[:20]}"
        keyboard.append([
            {"text": text, "callback_data": f"reminder_detail_{reminder.id}"}
        ])

    keyboard.append([{"text": "返回", "callback_data": "reminder_menu"}])

    return keyboard


def build_completed_detail_keyboard(reminder: Reminder) -> list[list[dict[str, Any]]]:
    """
    构建已完成提醒详情键盘（只保留删除和返回）。

    Args:
        reminder: 提醒对象

    Returns:
        InlineKeyboard 按钮列表
    """
    return [
        [{"text": "🗑️ 删除", "callback_data": f"reminder_delete_{reminder.id}"}],
        [{"text": "返回历史", "callback_data": "reminder_completed"}],
    ]


def format_completed_reminders(reminders: list[Reminder]) -> str:
    """
    格式化已完成提醒列表文本。

    Args:
        reminders: 已完成提醒列表

    Returns:
        格式化的文本
    """
    if not reminders:
        return "📜 暂无历史记录\n\n所有已触发或过期的提醒会显示在这里。"

    lines = ["📜 **历史记录**\n"]

    for i, reminder in enumerate(reminders[:8], 1):
        status = "✅ 已提醒" if reminder.triggered else "⏰ 已过期"
        if reminder.triggered_at:
            time_str = reminder.triggered_at.strftime("%m-%d %H:%M")
        else:
            time_str = reminder.created_at.strftime("%m-%d %H:%M")
        lines.append(f"{i}. {reminder.content}")
        lines.append(f"   {status} · {time_str}")

    if len(reminders) > 8:
        lines.append(f"\n... 还有 {len(reminders) - 8} 条记录")

    return "\n".join(lines)
