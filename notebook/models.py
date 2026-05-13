"""记事本数据模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BlockType(Enum):
    """记事内容块类型。"""

    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"


@dataclass
class NoteBlock:
    """记事内容块，支持文本、图片、语音混合。"""

    type: BlockType
    content: str = ""  # 文本内容或转写文本
    file_path: str | None = None  # 图片/语音文件的本地路径
    duration: int | None = None  # 语音时长（秒）
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "type": self.type.value,
            "content": self.content,
            "file_path": self.file_path,
            "duration": self.duration,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoteBlock":
        """从字典创建实例。"""
        return cls(
            type=BlockType(data.get("type", "text")),
            content=data.get("content", ""),
            file_path=data.get("file_path"),
            duration=data.get("duration"),
            metadata=data.get("metadata", {}),
        )

    def is_text(self) -> bool:
        return self.type == BlockType.TEXT

    def is_image(self) -> bool:
        return self.type == BlockType.IMAGE

    def is_voice(self) -> bool:
        return self.type == BlockType.VOICE


@dataclass
class Note:
    """记事本数据模型。"""

    id: str
    chat_id: int
    title: str = ""  # 笔记标题
    blocks: list[NoteBlock] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "title": self.title,
            "blocks": [b.to_dict() for b in self.blocks],
            "tags": self.tags,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Note":
        """从字典创建实例。"""
        return cls(
            id=data["id"],
            chat_id=data["chat_id"],
            title=data.get("title", ""),
            blocks=[NoteBlock.from_dict(b) for b in data.get("blocks", [])],
            tags=data.get("tags", []),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
            metadata=data.get("metadata", {}),
        )

    def get_summary(self, max_length: int = 50) -> str:
        """返回内容摘要。"""
        if not self.blocks:
            return "[空笔记]"

        parts = []
        for block in self.blocks:
            if block.is_text() and block.content:
                parts.append(block.content)
            elif block.is_image():
                parts.append("[图片]")
            elif block.is_voice():
                parts.append("🎤 [语音]")

        text = " ".join(parts)
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."

    def get_all_text(self) -> str:
        """返回所有文本内容（用于搜索）。"""
        texts = []
        for block in self.blocks:
            if block.content:
                texts.append(block.content)
        return " ".join(texts)

    def get_tag_text(self) -> str:
        """返回标签文本。"""
        if not self.tags:
            return ""
        return " ".join(f"#{tag}" for tag in self.tags)

    def has_voice(self) -> bool:
        """判断是否包含语音块。"""
        return any(b.is_voice() for b in self.blocks)

    def has_image(self) -> bool:
        """判断是否包含图片块。"""
        return any(b.is_image() for b in self.blocks)
