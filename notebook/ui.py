"""记事本模块的 InlineKeyboard UI 生成。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from notebook.models import Note, NoteBlock, BlockType


def build_main_menu_keyboard() -> list[list[dict[str, Any]]]:
    """构建记事本主菜单键盘。"""
    return [
        [{"text": "新建笔记", "callback_data": "notebook_create"}],
        [{"text": "查看笔记列表", "callback_data": "notebook_list"}],
        [{"text": "搜索笔记", "callback_data": "notebook_search"}],
        [{"text": "日期过滤", "callback_data": "notebook_date_filter"}],
        [{"text": "标签列表", "callback_data": "notebook_tags"}],
        [{"text": "退出", "callback_data": "notebook_exit"}],
    ]


def build_note_list_keyboard(notes: list[Note]) -> list[list[dict[str, Any]]]:
    """构建笔记列表键盘。"""
    keyboard: list[list[dict[str, Any]]] = []

    for note in notes[:10]:  # 最多显示 10 个
        text = note.get_summary(20)
        keyboard.append([
            {"text": text, "callback_data": f"notebook_detail_{note.id}"},
            {"text": "🗑️", "callback_data": f"notebook_delete_{note.id}"},
        ])

    keyboard.append([{"text": "返回", "callback_data": "notebook_menu"}])

    return keyboard


def build_note_detail_keyboard(note: Note) -> list[list[dict[str, Any]]]:
    """构建笔记详情键盘。"""
    keyboard: list[list[dict[str, Any]]] = []

    keyboard.append([
        {"text": "✏️ 编辑", "callback_data": f"notebook_edit_{note.id}"},
        {"text": "🗑️ 删除", "callback_data": f"notebook_delete_{note.id}"},
    ])
    keyboard.append([
        {"text": "返回列表", "callback_data": "notebook_list"},
    ])

    return keyboard


def build_delete_confirm_keyboard(note_id: str) -> list[list[dict[str, Any]]]:
    """构建删除确认键盘。"""
    return [
        [
            {"text": "确认删除", "callback_data": f"notebook_delete_confirm_{note_id}"},
            {"text": "取消", "callback_data": f"notebook_detail_{note_id}"},
        ]
    ]


def build_editing_keyboard() -> list[list[dict[str, Any]]]:
    """构建编辑中的键盘（结束编辑按钮）。"""
    return [
        [{"text": "🧹 清空重写", "callback_data": "notebook_rewrite"}],
        [{"text": "✅ 结束编辑", "callback_data": "notebook_finish_edit"}],
    ]


def format_note_list(notes: list[Note]) -> str:
    """格式化笔记列表文本。"""
    if not notes:
        return "暂无笔记"

    lines: list[str] = []
    for i, note in enumerate(notes, 1):
        tag_text = note.get_tag_text()
        created = note.created_at.strftime("%m-%d %H:%M") if note.created_at else ""
        # 根据内容类型显示不同标记
        if note.has_voice():
            marker = "🎤"
        elif note.has_image():
            marker = "🖼️"
        else:
            marker = "📝"
        line = f"{i}. {marker} {note.get_summary(25)}"
        if tag_text:
            line += f" {tag_text}"
        if created:
            line += f" ({created})"
        lines.append(line)

    return "\n".join(lines)


def format_note_detail(note: Note) -> str:
    """格式化笔记详情文本。"""
    lines: list[str] = []

    # 标题
    if note.title:
        lines.append(f"📌 {note.title}")
        lines.append("")

    # 内容块
    for block in note.blocks:
        if block.is_text():
            lines.append(block.content)
        elif block.is_image():
            image_name = Path(block.file_path).name if block.file_path else "[图片]"
            lines.append(f"🖼️ {image_name}")
        elif block.is_voice():
            duration_str = f"{block.duration}s" if block.duration else ""
            lines.append(f"🎤 [语音 {duration_str}] {block.content}")
        lines.append("")

    if note.tags:
        lines.append(f"标签：{note.get_tag_text()}")

    if note.created_at:
        lines.append(f"创建：{note.created_at.strftime('%Y-%m-%d %H:%M')}")

    if note.updated_at:
        lines.append(f"更新：{note.updated_at.strftime('%Y-%m-%d %H:%M')}")

    return "\n".join(lines)


def format_editing_preview(blocks: list[NoteBlock]) -> str:
    """格式化编辑中的预览文本。"""
    if not blocks:
        return "当前笔记为空，请发送内容..."

    lines = ["当前笔记内容：", ""]
    for i, block in enumerate(blocks, 1):
        if block.is_text():
            lines.append(f"{i}. 📝 {block.content[:50]}{'...' if len(block.content) > 50 else ''}")
        elif block.is_image():
            image_name = Path(block.file_path).name if block.file_path else "[图片]"
            lines.append(f"{i}. 🖼️ {image_name}")
        elif block.is_voice():
            duration_str = f"{block.duration}s" if block.duration else ""
            lines.append(f"{i}. 🎤 [语音 {duration_str}] {block.content[:30]}{'...' if len(block.content) > 30 else ''}")

    lines.append("")
    lines.append("继续发送内容追加，点击「清空重写」可整体重写，或点击「结束编辑」保存。")
    return "\n".join(lines)


def parse_tags(text: str) -> tuple[str, list[str]]:
    """从文本中解析标签。

    标签格式：#标签名（中文或英文，不含空格）

    Returns:
        (纯文本内容, 标签列表)
    """
    import re

    # 匹配 #标签 格式
    tag_pattern = r'#([\u4e00-\u9fa5a-zA-Z0-9_\-]+)'
    tags = re.findall(tag_pattern, text)

    # 移除文本中的标签
    content = re.sub(tag_pattern, '', text).strip()
    # 清理多余空格
    content = re.sub(r'\s+', ' ', content)

    return content, tags


def parse_search_query(text: str) -> tuple[str, list[str], datetime | None, datetime | None]:
    """解析搜索查询。

    支持格式：
    - 普通关键词
    - #标签
    - 日期范围：YYYY-MM-DD YYYY-MM-DD
    - 组合使用

    Returns:
        (关键词, 标签列表, 开始日期, 结束日期)
    """
    import re

    keyword = ""
    tags: list[str] = []
    start_date: datetime | None = None
    end_date: datetime | None = None

    # 提取标签
    tag_pattern = r'#([\u4e00-\u9fa5a-zA-Z0-9_\-]+)'
    tags = re.findall(tag_pattern, text)
    text_without_tags = re.sub(tag_pattern, '', text).strip()

    # 提取日期范围
    date_pattern = r'(\d{4}-\d{2}-\d{2})'
    dates = re.findall(date_pattern, text_without_tags)

    if len(dates) >= 2:
        try:
            start_date = datetime.strptime(dates[0], "%Y-%m-%d")
            end_date = datetime.strptime(dates[1], "%Y-%m-%d")
            # 移除日期部分
            text_without_dates = re.sub(date_pattern, '', text_without_tags).strip()
            keyword = re.sub(r'\s+', ' ', text_without_dates).strip()
        except ValueError:
            keyword = text_without_tags
    elif len(dates) == 1:
        try:
            start_date = datetime.strptime(dates[0], "%Y-%m-%d")
            end_date = start_date + timedelta(days=1)
            text_without_dates = re.sub(date_pattern, '', text_without_tags).strip()
            keyword = re.sub(r'\s+', ' ', text_without_dates).strip()
        except ValueError:
            keyword = text_without_tags
    else:
        keyword = text_without_tags

    return keyword, tags, start_date, end_date


def parse_date_range(text: str) -> tuple[datetime, datetime]:
    """解析日期范围。

    Args:
        text: 格式 "YYYY-MM-DD YYYY-MM-DD"

    Returns:
        (开始日期, 结束日期)

    Raises:
        ValueError: 格式错误
    """
    import re

    parts = text.strip().split()
    if len(parts) != 2:
        raise ValueError("请输入两个日期，格式：YYYY-MM-DD YYYY-MM-DD")

    start_date = datetime.strptime(parts[0], "%Y-%m-%d")
    end_date = datetime.strptime(parts[1], "%Y-%m-%d")
    # 结束日期设为当天最后一秒
    end_date = end_date.replace(hour=23, minute=59, second=59)

    return start_date, end_date
