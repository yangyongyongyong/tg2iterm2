"""记事本管理器，封装 SQLite 持久化。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from notebook.models import BlockType, Note, NoteBlock


class NoteManager:
    """记事本管理器，封装 SQLite 的所有操作。"""

    def __init__(self, db_path: Path | str) -> None:
        """初始化记事本管理器。

        Args:
            db_path: SQLite 数据库路径
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构。"""
        with sqlite3.connect(self._db_path) as conn:
            # 笔记主表
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    title TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            # 笔记内容块表（支持文本/图片/语音混合）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS note_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL,
                    block_type TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    file_path TEXT,
                    duration INTEGER,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            # 创建索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_chat_id ON notes(chat_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_created_at ON notes(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_note_blocks_note_id ON note_blocks(note_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_note_blocks_content ON note_blocks(content)")
            conn.commit()

    def add_note(
        self,
        chat_id: int,
        title: str = "",
        blocks: list[NoteBlock] | None = None,
        tags: list[str] | None = None,
    ) -> Note:
        """添加新笔记。

        Args:
            chat_id: Telegram Chat ID
            title: 笔记标题
            blocks: 内容块列表
            tags: 标签列表

        Returns:
            创建的 Note 实例
        """
        note_id = str(uuid.uuid4())
        now = datetime.now()
        note = Note(
            id=note_id,
            chat_id=chat_id,
            title=title,
            blocks=blocks or [],
            tags=tags or [],
            created_at=now,
            updated_at=now,
        )
        with sqlite3.connect(self._db_path) as conn:
            # 插入笔记主表
            conn.execute(
                """
                INSERT INTO notes (id, chat_id, title, tags, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.id,
                    note.chat_id,
                    note.title,
                    json.dumps(note.tags),
                    note.created_at.isoformat(),
                    note.updated_at.isoformat() if note.updated_at else None,
                    json.dumps(note.metadata),
                ),
            )
            # 插入内容块
            for i, block in enumerate(note.blocks):
                conn.execute(
                    """
                    INSERT INTO note_blocks (note_id, block_type, content, file_path, duration, sort_order, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note.id,
                        block.type.value,
                        block.content,
                        block.file_path,
                        block.duration,
                        i,
                        json.dumps(block.metadata),
                    ),
                )
            conn.commit()
        return note

    def get_note(self, note_id: str) -> Note | None:
        """获取单个笔记。

        Args:
            note_id: 笔记 ID

        Returns:
            Note 实例，不存在返回 None
        """
        with sqlite3.connect(self._db_path) as conn:
            # 获取笔记主表
            cursor = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
            row = cursor.fetchone()
            if row is None:
                return None

            # 获取内容块
            blocks_cursor = conn.execute(
                "SELECT * FROM note_blocks WHERE note_id = ? ORDER BY sort_order",
                (note_id,),
            )
            blocks = [self._row_to_block(b) for b in blocks_cursor.fetchall()]

            return self._row_to_note(row, blocks)

    def get_all_notes(self, chat_id: int | None = None) -> list[Note]:
        """获取所有笔记。

        Args:
            chat_id: 可选，过滤特定 chat_id

        Returns:
            Note 列表，按创建时间倒序
        """
        with sqlite3.connect(self._db_path) as conn:
            if chat_id is not None:
                cursor = conn.execute(
                    "SELECT * FROM notes WHERE chat_id = ? ORDER BY created_at DESC",
                    (chat_id,),
                )
            else:
                cursor = conn.execute("SELECT * FROM notes ORDER BY created_at DESC")
            rows = cursor.fetchall()

            notes = []
            for row in rows:
                note_id = row[0]
                blocks_cursor = conn.execute(
                    "SELECT * FROM note_blocks WHERE note_id = ? ORDER BY sort_order",
                    (note_id,),
                )
                blocks = [self._row_to_block(b) for b in blocks_cursor.fetchall()]
                notes.append(self._row_to_note(row, blocks))

        return notes

    def search_notes(
        self,
        chat_id: int | None = None,
        keyword: str = "",
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        tags: list[str] | None = None,
    ) -> list[Note]:
        """检索笔记。

        搜索范围包括：所有内容块的 content。

        Args:
            chat_id: 可选，过滤特定 chat_id
            keyword: 关键词，匹配所有内容块的 content
            start_date: 可选，开始日期（包含）
            end_date: 可选，结束日期（包含）
            tags: 可选，标签过滤

        Returns:
            匹配的 Note 列表，按创建时间倒序
        """
        # 先获取所有笔记，然后在内存中过滤
        notes = self.get_all_notes(chat_id=chat_id)

        if keyword:
            keyword_lower = keyword.lower()
            filtered = []
            for note in notes:
                # 搜索标题
                if keyword_lower in note.title.lower():
                    filtered.append(note)
                    continue
                # 搜索所有内容块
                for block in note.blocks:
                    if block.content and keyword_lower in block.content.lower():
                        filtered.append(note)
                        break
            notes = filtered

        if start_date is not None:
            notes = [n for n in notes if n.created_at and n.created_at >= start_date]

        if end_date is not None:
            notes = [n for n in notes if n.created_at and n.created_at <= end_date]

        if tags:
            notes = [n for n in notes if all(t in n.tags for t in tags)]

        return notes

    def update_note(self, note_id: str, title: str | None = None, blocks: list[NoteBlock] | None = None, tags: list[str] | None = None) -> Note | None:
        """更新笔记。

        Args:
            note_id: 笔记 ID
            title: 可选，新标题
            blocks: 可选，新内容块列表（会替换原有内容块）
            tags: 可选，新标签

        Returns:
            更新后的 Note 实例，不存在返回 None
        """
        note = self.get_note(note_id)
        if note is None:
            return None

        updates: list[str] = []
        params: list[Any] = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
            note.title = title

        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags))
            note.tags = tags

        if not updates and blocks is None:
            return note

        with sqlite3.connect(self._db_path) as conn:
            # 更新笔记主表
            if updates:
                updates.append("updated_at = ?")
                params.append(datetime.now().isoformat())
                params.append(note_id)
                conn.execute(
                    f"UPDATE notes SET {', '.join(updates)} WHERE id = ?",
                    tuple(params),
                )

            # 如果提供了新的内容块，替换原有内容块
            if blocks is not None:
                # 删除旧的内容块
                conn.execute("DELETE FROM note_blocks WHERE note_id = ?", (note_id,))
                # 插入新的内容块
                for i, block in enumerate(blocks):
                    conn.execute(
                        """
                        INSERT INTO note_blocks (note_id, block_type, content, file_path, duration, sort_order, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            note_id,
                            block.type.value,
                            block.content,
                            block.file_path,
                            block.duration,
                            i,
                            json.dumps(block.metadata),
                        ),
                    )
                note.blocks = blocks

            conn.commit()

        return self.get_note(note_id)

    def delete_note(self, note_id: str) -> bool:
        """删除笔记。

        Args:
            note_id: 笔记 ID

        Returns:
            是否成功删除
        """
        with sqlite3.connect(self._db_path) as conn:
            # 先删除内容块
            conn.execute("DELETE FROM note_blocks WHERE note_id = ?", (note_id,))
            # 再删除笔记主表
            cursor = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_notes_by_date_range(
        self,
        chat_id: int,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Note]:
        """根据日期范围获取笔记。

        Args:
            chat_id: Telegram Chat ID
            start_date: 开始日期（包含）
            end_date: 结束日期（包含）

        Returns:
            Note 列表，按创建时间倒序
        """
        return self.search_notes(chat_id=chat_id, start_date=start_date, end_date=end_date)

    def get_all_tags(self, chat_id: int | None = None) -> list[str]:
        """获取所有标签。

        Args:
            chat_id: 可选，过滤特定 chat_id

        Returns:
            标签列表（去重、排序）
        """
        notes = self.get_all_notes(chat_id=chat_id)
        tags: set[str] = set()
        for note in notes:
            tags.update(note.tags)
        return sorted(list(tags))

    def _row_to_note(self, row: tuple, blocks: list[NoteBlock] | None = None) -> Note:
        """将数据库行转换为 Note 实例。"""
        return Note(
            id=row[0],
            chat_id=row[1],
            title=row[2] if len(row) > 2 else "",
            tags=json.loads(row[3]) if len(row) > 3 and row[3] else [],
            created_at=datetime.fromisoformat(row[4]) if len(row) > 4 and row[4] else datetime.now(),
            updated_at=datetime.fromisoformat(row[5]) if len(row) > 5 and row[5] else None,
            metadata=json.loads(row[6]) if len(row) > 6 and row[6] else {},
            blocks=blocks or [],
        )

    def _row_to_block(self, row: tuple) -> NoteBlock:
        """将数据库行转换为 NoteBlock 实例。"""
        return NoteBlock(
            type=BlockType(row[2]) if len(row) > 2 else BlockType.TEXT,
            content=row[3] if len(row) > 3 else "",
            file_path=row[4] if len(row) > 4 else None,
            duration=row[5] if len(row) > 5 else None,
            metadata=json.loads(row[7]) if len(row) > 7 and row[7] else {},
        )
