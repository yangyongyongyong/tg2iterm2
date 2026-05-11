"""自定义触发器。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from apscheduler.triggers.base import BaseTrigger


class NthWeekdayTrigger(BaseTrigger):
    """
    每月第 N 个星期 X 触发器。

    支持排除特定月份。

    示例：
    - 每月第二个星期日: NthWeekdayTrigger(nth=2, weekday=6)
    - 每月第一个周一（排除1月）: NthWeekdayTrigger(nth=1, weekday=0, exclude_months=[1])
    """

    WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def __init__(
        self,
        nth: int,
        weekday: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        exclude_months: list[int] | None = None,
        timezone: str | None = None,
    ):
        """
        初始化触发器。

        Args:
            nth: 第几个（1=第一个，2=第二个，...，-1=最后一个）
            weekday: 星期几（0=周一，6=周日）
            hour: 小时（0-23）
            minute: 分钟（0-59）
            second: 秒（0-59）
            exclude_months: 排除的月份列表（1-12）
            timezone: 时区
        """
        if nth < -1 or nth > 5:
            raise ValueError("nth must be between -1 and 5")
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday must be between 0 and 6")

        self.nth = nth
        self.weekday = weekday
        self.hour = hour
        self.minute = minute
        self.second = second
        self.exclude_months = exclude_months or []
        self.timezone = timezone

    def _find_nth_weekday(self, year: int, month: int) -> datetime | None:
        """
        找到指定年月中第 N 个星期 X。

        Returns:
            找到的日期，如果月份被排除则返回 None
        """
        if month in self.exclude_months:
            return None

        # 获取该月第一天
        first_day = datetime(year, month, 1)

        # 找到第一个目标星期 X
        days_until_weekday = (self.weekday - first_day.weekday()) % 7
        first_weekday = first_day + timedelta(days=days_until_weekday)

        if self.nth == -1:
            # 最后一个：从月末往前找
            if month == 12:
                next_month = datetime(year + 1, 1, 1)
            else:
                next_month = datetime(year, month + 1, 1)
            last_day = next_month - timedelta(days=1)

            # 找到最后一个目标星期 X
            days_back = (last_day.weekday() - self.weekday) % 7
            target_day = last_day - timedelta(days=days_back)
        else:
            # 第 N 个
            target_day = first_weekday + timedelta(weeks=self.nth - 1)

            # 检查是否超出该月
            if target_day.month != month:
                return None

        return datetime(target_day.year, target_day.month, target_day.day, self.hour, self.minute, self.second)

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime | None:
        """
        计算下一次触发时间。

        Args:
            previous_fire_time: 上次触发时间
            now: 当前时间

        Returns:
            下一次触发时间，如果没有则返回 None
        """
        # 处理时区：如果 now 是 aware 的，我们需要在比较时使用相同的时区
        from pytz import UTC
        is_aware = now.tzinfo is not None and now.tzinfo.utcoffset(now) is not None
        
        if previous_fire_time:
            # 从上次触发时间的下个月开始搜索
            search_start = previous_fire_time.replace(day=1) + timedelta(days=32)
            search_start = search_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            # 从当前月份开始搜索
            search_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # 最多搜索 24 个月
        for _ in range(24):
            year = search_start.year
            month = search_start.month

            target_datetime = self._find_nth_weekday(year, month)

            if target_datetime:
                # 如果 now 是 aware 的，将 target_datetime 也转换为 aware
                if is_aware:
                    target_datetime = target_datetime.replace(tzinfo=now.tzinfo)
                
                if target_datetime > now:
                    return target_datetime

            # 移动到下个月
            if month == 12:
                search_start = search_start.replace(year=year + 1, month=1)
            else:
                search_start = search_start.replace(month=month + 1)

        return None

    def __repr__(self) -> str:
        weekday_name = self.WEEKDAY_NAMES[self.weekday]
        if self.nth == -1:
            nth_str = "last"
        else:
            nth_str = f"{self.nth}th"
        return f"NthWeekdayTrigger({nth_str} {weekday_name} at {self.hour:02d}:{self.minute:02d})"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "nth": self.nth,
            "weekday": self.weekday,
            "hour": self.hour,
            "minute": self.minute,
            "second": self.second,
            "exclude_months": self.exclude_months,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NthWeekdayTrigger":
        """从字典创建实例。"""
        return cls(
            nth=data["nth"],
            weekday=data["weekday"],
            hour=data.get("hour", 0),
            minute=data.get("minute", 0),
            second=data.get("second", 0),
            exclude_months=data.get("exclude_months", []),
            timezone=data.get("timezone"),
        )
