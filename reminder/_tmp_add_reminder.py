#!/usr/bin/env python3
"""临时脚本：三分钟后刷牙提醒，执行后删除。"""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from reminder.manager import ReminderManager  # noqa: E402

DB_PATH = Path.home() / ".tg2iterm2" / "reminders.db"
CHAT_ID = 1151534243


async def main() -> None:
    """添加一次性日期提醒并打印结果 JSON。"""
    from datetime import datetime

    manager = ReminderManager(db_path=DB_PATH)
    await manager.start()
    try:
        run_time = datetime.now() + timedelta(minutes=3)
        result = await manager.add_reminder(
            chat_id=CHAT_ID,
            content="刷牙",
            trigger_type="date",
            trigger_config={"run_date": run_time.isoformat(timespec="seconds")},
        )
        print(
            json.dumps(
                {
                    "success": True,
                    "reminder_id": result.id,
                    "run_date": run_time.isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            )
        )
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
