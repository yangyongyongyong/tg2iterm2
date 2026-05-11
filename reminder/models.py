"""提醒数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Reminder:
    """提醒数据模型。"""

    id: str
    chat_id: int
    content: str
    trigger_type: str  # date / cron / nth_weekday
    trigger_config: dict[str, Any]
    created_at: datetime
    next_fire_time: datetime | None = None
    paused: bool = False
    triggered: bool = False  # 是否已触发
    expired: bool = False  # 是否已过期（错过触发时间）
    triggered_at: datetime | None = None  # 实际触发时间
    info: str = ""  # 用户备注信息
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "content": self.content,
            "trigger_type": self.trigger_type,
            "trigger_config": self.trigger_config,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "next_fire_time": self.next_fire_time.isoformat() if self.next_fire_time else None,
            "paused": self.paused,
            "triggered": self.triggered,
            "expired": self.expired,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "info": self.info,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reminder":
        """从字典创建实例。"""
        return cls(
            id=data["id"],
            chat_id=data["chat_id"],
            content=data["content"],
            trigger_type=data["trigger_type"],
            trigger_config=data["trigger_config"],
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            next_fire_time=datetime.fromisoformat(data["next_fire_time"]) if data.get("next_fire_time") else None,
            paused=data.get("paused", False),
            triggered=data.get("triggered", False),
            expired=data.get("expired", False),
            triggered_at=datetime.fromisoformat(data["triggered_at"]) if data.get("triggered_at") else None,
            info=data.get("info", ""),
            metadata=data.get("metadata", {}),
        )

    def is_active(self) -> bool:
        """检查提醒是否有效（未触发、未过期、未暂停、未过时）。"""
        if self.triggered or self.expired or self.paused:
            return False
        # 一次性 date 提醒：如果设定时间已过，视为无效
        if self.trigger_type == "date":
            run_date = self.trigger_config.get("run_date")
            if run_date:
                if isinstance(run_date, str):
                    run_date = datetime.fromisoformat(run_date)
                if run_date.tzinfo:
                    run_date = run_date.astimezone().replace(tzinfo=None)
                if run_date < datetime.now():
                    return False
        return True

    def get_human_readable_schedule(self) -> str:
        """返回人类可读的调度描述。"""
        # 状态前缀
        status_prefix = ""
        if self.triggered:
            status_prefix = "✅ 已提醒 "
        elif self.expired:
            status_prefix = "⏰ 已过期 "
        elif self.paused:
            status_prefix = "⏸️ 已暂停 "

        if self.trigger_type == "date":
            dt = self.trigger_config.get("run_date")
            if dt:
                if isinstance(dt, str):
                    try:
                        dt = datetime.fromisoformat(dt)
                    except Exception:
                        return f"{status_prefix}一次性提醒 {dt}"
                if isinstance(dt, datetime):
                    now = datetime.now()
                    diff = dt - now
                    if diff.total_seconds() > 0 and not self.triggered and not self.expired:
                        if diff.total_seconds() < 60:
                            return f"{int(diff.total_seconds())} 秒后"
                        elif diff.total_seconds() < 3600:
                            return f"{int(diff.total_seconds() / 60)} 分钟后"
                        elif diff.total_seconds() < 86400:
                            return f"{int(diff.total_seconds() / 3600)} 小时后"
                    return f"{status_prefix}{dt.strftime('%m-%d %H:%M')}"
            return f"{status_prefix}一次性提醒"

        if self.trigger_type == "cron":
            parts = []
            if self.trigger_config.get("day_of_week"):
                days = self.trigger_config["day_of_week"]
                if isinstance(days, str):
                    day_names = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四", "fri": "周五", "sat": "周六", "sun": "周日"}
                    parts.append(day_names.get(days, days))
            if self.trigger_config.get("hour") is not None:
                hour = self.trigger_config["hour"]
                minute = self.trigger_config.get("minute", 0)
                parts.append(f"{hour:02d}:{minute:02d}")
            if not parts:
                return f"{status_prefix}周期提醒"
            return f"{status_prefix}{' '.join(parts)}"

        if self.trigger_type == "nth_weekday":
            nth = self.trigger_config.get("nth", 1)
            weekday = self.trigger_config.get("weekday", 0)
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            nth_names = ["第一个", "第二个", "第三个", "第四个", "最后一个"]
            return f"{status_prefix}每月{nth_names[nth - 1] if nth <= 5 else f'第{nth}个'}{weekday_names[weekday]}"

        return f"{status_prefix}自定义提醒"
