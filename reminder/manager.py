"""提醒管理器，封装 APScheduler 3.x。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from reminder.models import Reminder
from reminder.triggers import NthWeekdayTrigger


# 全局回调函数注册表（支持异步回调）
_callback_registry: dict[str, Callable[[Reminder], Any] | Callable[[Reminder], Awaitable[Any]]] = {}
# 全局管理器引用（用于更新状态）
_manager_ref: ReminderManager | None = None


# 全局事件循环引用
_event_loop: asyncio.AbstractEventLoop | None = None


async def _reminder_job_func(reminder_id: str, metadata_json: str) -> None:
    """提醒触发的异步任务函数 - 由 APScheduler 直接在事件循环中调用。"""
    print(f"[提醒已触发] 触发: {reminder_id}")
    await _async_reminder_handler(reminder_id, metadata_json)


async def _async_reminder_handler(reminder_id: str, metadata_json: str) -> None:
    """异步处理提醒触发。"""
    try:
        metadata = json.loads(metadata_json)
        reminder = Reminder.from_dict(metadata)

        # 无论有没有回调，都标记为已触发并保存，防止重复触发
        reminder.triggered = True
        reminder.triggered_at = datetime.now()
        if _manager_ref:
            _manager_ref._reminders[reminder_id] = reminder
            await _manager_ref._save_reminder_record(reminder)

        callback = _callback_registry.get(reminder_id)
        if callback:
            result = callback(reminder)
            if asyncio.iscoroutine(result):
                await result
            print(f"[提醒已触发] 完成: {reminder.content}")
        else:
            print(f"[提醒已触发] 未注册: {reminder_id}")
    except Exception as e:
        print(f"[提醒已触发] 错误: {e}")


class ReminderManager:
    """提醒管理器，封装 APScheduler 3.x 的所有操作。"""

    def __init__(self, db_path: Path | str, on_reminder: Callable[[Reminder], Any] | None = None):
        """
        初始化提醒管理器。

        Args:
            db_path: SQLite 数据库路径
            on_reminder: 提醒触发时的回调函数（支持同步和异步）
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._records_path = self._db_path.parent / "reminder_records.json"  # 持久化记录
        self._on_reminder = on_reminder
        self._scheduler: AsyncIOScheduler | None = None
        self._reminders: dict[str, Reminder] = {}
        self._lock = asyncio.Lock()
        
        # 注册全局管理器引用
        global _manager_ref
        _manager_ref = self

    async def start(self) -> None:
        """启动调度器。"""
        if self._scheduler is not None:
            return

        # 保存事件循环引用，供回调使用
        global _event_loop
        _event_loop = asyncio.get_running_loop()

        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{self._db_path}')
        }
        # 配置调度器
        job_defaults = {
            'misfire_grace_time': None,  # 永不过期，即使错过也执行
            'coalesce': True,  # 合并错过的任务
        }
        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults=job_defaults,
        )

        # 添加事件监听器用于调试
        self._scheduler.add_listener(self._on_scheduler_event)

        # 先以 paused 模式启动，初始化 job store，再加载回调，最后 resume
        # 避免启动瞬间的 misfired 任务在回调注册前被触发
        self._scheduler.start(paused=True)
        await self._load_reminders()
        self._scheduler.resume()
        print(f"[ReminderManager] 启动完成: 事件循环={_event_loop is not None}, 提醒数={len(self._reminders)}")
    
    def _on_scheduler_event(self, event) -> None:
        """APScheduler 事件监听器。"""
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
        
        if event.code == EVENT_JOB_ERROR:
            print(f"[提醒已触发] 任务错误: {getattr(event, 'job_id', '?')} {getattr(event, 'exception', '')}")
        elif event.code == EVENT_JOB_MISSED:
            print(f"[提醒已触发] 任务错过: {getattr(event, 'job_id', '?')}")
        elif event.code == EVENT_JOB_EXECUTED:
            print(f"[提醒已触发] 任务完成: {getattr(event, 'job_id', '?')}")
        elif event.code == EVENT_JOB_SUBMITTED:
            print(f"[提醒已触发] 任务提交: {getattr(event, 'job_id', '?')}")

    async def stop(self) -> None:
        """停止调度器。"""
        # 保存所有记录
        await self._save_all_records()
        
        if self._scheduler is not None:
            self._scheduler.shutdown()
            self._scheduler = None
        # 清理回调注册
        global _callback_registry
        for rid in list(self._reminders.keys()):
            _callback_registry.pop(rid, None)

    async def _load_reminders(self) -> None:
        """从数据库加载已有提醒。"""
        if self._scheduler is None:
            return
        
        # 先清理内存中的旧提醒和回调
        global _callback_registry
        for rid in list(self._reminders.keys()):
            _callback_registry.pop(rid, None)
        self._reminders.clear()
        
        jobs = self._scheduler.get_jobs()
        print(f"[ReminderManager] 加载提醒，任务数: {len(jobs)}")
        
        # 先加载已有记录
        await self._load_reminder_records()
        
        for job in jobs:
            # 从 job.args 中提取 metadata
            if len(job.args) >= 2:
                reminder_id = job.args[0]
                metadata_json = job.args[1]
                try:
                    metadata = json.loads(metadata_json)
                    reminder = Reminder.from_dict(metadata)
                    
                    # 检查是否已有记录（可能已触发/过期）
                    if reminder_id in self._reminders:
                        existing = self._reminders[reminder_id]
                        # 保留已触发/过期状态
                        reminder.triggered = existing.triggered
                        reminder.expired = existing.expired
                        reminder.triggered_at = existing.triggered_at
                    
                    # 一次性 date 提醒时间已过但未标记过期的，自动标记
                    if (reminder.trigger_type == "date"
                            and not reminder.triggered
                            and not reminder.expired):
                        run_date = reminder.trigger_config.get("run_date")
                        if run_date:
                            if isinstance(run_date, str):
                                run_date = datetime.fromisoformat(run_date)
                            if run_date.tzinfo:
                                run_date = run_date.astimezone().replace(tzinfo=None)
                            if run_date < datetime.now():
                                reminder.expired = True
                                await self._save_reminder_record(reminder)

                    self._reminders[reminder_id] = reminder
                    # 注册回调（只对有效的提醒）
                    if self._on_reminder and reminder.is_active():
                        _callback_registry[reminder_id] = self._on_reminder
                except Exception as e:
                    print(f"[ReminderManager] 加载错误: {e}")

    async def _load_reminder_records(self) -> None:
        """从 JSON 文件加载提醒记录。"""
        if not self._records_path.exists():
            return
        
        def _read():
            with open(self._records_path, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            records = await asyncio.to_thread(_read)
            for rid, data in records.items():
                self._reminders[rid] = Reminder.from_dict(data)
            print(f"[ReminderManager] 加载记录数: {len(records)}")
        except Exception as e:
            print(f"Load reminder records error: {e}")

    async def _save_reminder_record(self, reminder: Reminder) -> None:
        """保存单个提醒记录到 JSON 文件。"""
        reminder_dict = reminder.to_dict()
        async with self._lock:
            try:
                def _write():
                    records = {}
                    if self._records_path.exists():
                        with open(self._records_path, "r", encoding="utf-8") as f:
                            records = json.load(f)
                    records[reminder.id] = reminder_dict
                    with open(self._records_path, "w", encoding="utf-8") as f:
                        json.dump(records, f, ensure_ascii=False, indent=2)
                await asyncio.to_thread(_write)
            except Exception as e:
                print(f"Save reminder record error: {e}")

    async def _save_all_records(self) -> None:
        """批量保存所有提醒记录到 JSON 文件。"""
        records = {rid: r.to_dict() for rid, r in self._reminders.items()}
        async with self._lock:
            try:
                def _write():
                    with open(self._records_path, "w", encoding="utf-8") as f:
                        json.dump(records, f, ensure_ascii=False, indent=2)
                await asyncio.to_thread(_write)
            except Exception as e:
                print(f"Save all records error: {e}")

    async def reload_reminders(self) -> None:
        """重新从数据库加载提醒（用于同步外部创建的提醒）。"""
        print(f"[ReminderManager] reload_reminders: 清理旧数据并重新加载")

        # 在重新加载之前，检查 APScheduler 的任务状态
        if self._scheduler:
            jobs = self._scheduler.get_jobs()
            print(f"[ReminderManager] APScheduler 任务数: {len(jobs)}")
            for j in jobs:
                print(f"[ReminderManager]   Job: {j.id}, next_run={j.next_run_time}")

        await self._load_reminders()
        print(f"[ReminderManager] reload完成: 提醒数={len(self._reminders)}, 活跃数={self.get_reminder_count()}, 回调={list(_callback_registry.keys())}")

        # 强制调度器重新计算下次唤醒时间，否则可能睡过头错过新任务
        if self._scheduler:
            self._scheduler.wakeup()

    def _build_trigger(self, trigger_type: str, config: dict[str, Any]):
        """根据类型和配置构建 Trigger。"""
        if trigger_type == "date":
            run_date = config.get("run_date")
            if isinstance(run_date, str):
                run_date = datetime.fromisoformat(run_date)
            return DateTrigger(run_date=run_date, timezone=config.get("timezone"))

        if trigger_type == "cron":
            return CronTrigger(
                year=config.get("year"),
                month=config.get("month"),
                day=config.get("day"),
                week=config.get("week"),
                day_of_week=config.get("day_of_week"),
                hour=config.get("hour"),
                minute=config.get("minute"),
                second=config.get("second"),
                timezone=config.get("timezone"),
            )

        if trigger_type == "nth_weekday":
            return NthWeekdayTrigger(
                nth=config.get("nth", 1),
                weekday=config.get("weekday", 0),
                hour=config.get("hour", 0),
                minute=config.get("minute", 0),
                exclude_months=config.get("exclude_months", []),
            )

        if trigger_type == "interval":
            kwargs: dict[str, Any] = {
                "weeks": config.get("weeks", 0),
                "days": config.get("days", 0),
                "hours": config.get("hours", 0),
                "minutes": config.get("minutes", 0),
                "seconds": config.get("seconds", 0),
            }
            if config.get("timezone") is not None:
                kwargs["timezone"] = config.get("timezone")
            if config.get("jitter") is not None:
                kwargs["jitter"] = config.get("jitter")
            sd = config.get("start_date")
            if sd is not None:
                if isinstance(sd, str):
                    sd = datetime.fromisoformat(sd)
                kwargs["start_date"] = sd
            ed = config.get("end_date")
            if ed is not None:
                if isinstance(ed, str):
                    ed = datetime.fromisoformat(ed)
                kwargs["end_date"] = ed
            return IntervalTrigger(**kwargs)

        raise ValueError(f"不支持的触发器类型: {trigger_type}")

    async def add_reminder(
        self,
        chat_id: int,
        content: str,
        trigger_type: str,
        trigger_config: dict[str, Any],
    ) -> Reminder:
        """
        添加新提醒。

        Args:
            chat_id: Telegram Chat ID
            content: 提醒内容
            trigger_type: 触发器类型 (date / cron / nth_weekday / interval)
            trigger_config: 触发器配置

        Returns:
            创建的 Reminder 对象
        """
        reminder_id = str(uuid.uuid4())[:8]
        reminder = Reminder(
            id=reminder_id,
            chat_id=chat_id,
            content=content,
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            created_at=datetime.now(),
        )

        trigger = self._build_trigger(trigger_type, trigger_config)
        metadata_json = json.dumps(reminder.to_dict())

        # 注册回调
        global _callback_registry
        if self._on_reminder:
            _callback_registry[reminder_id] = self._on_reminder

        async with self._lock:
            self._reminders[reminder_id] = reminder
            self._scheduler.add_job(
                _reminder_job_func,
                trigger=trigger,
                id=reminder_id,
                args=[reminder_id, metadata_json],
            )
        
        # 异步保存记录
        await self._save_reminder_record(reminder)

        return reminder

    async def remove_reminder(self, reminder_id: str) -> bool:
        """删除提醒。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False

            try:
                self._scheduler.remove_job(reminder_id)
            except Exception:
                pass

            del self._reminders[reminder_id]
            global _callback_registry
            _callback_registry.pop(reminder_id, None)
            return True

    async def pause_reminder(self, reminder_id: str) -> bool:
        """暂停提醒。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False

            reminder = self._reminders[reminder_id]
            if reminder.paused:
                return False

            try:
                self._scheduler.pause_job(reminder_id)
                reminder.paused = True
                return True
            except Exception:
                return False

    async def resume_reminder(self, reminder_id: str) -> bool:
        """恢复提醒。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False

            reminder = self._reminders[reminder_id]
            if not reminder.paused:
                return False

            try:
                self._scheduler.resume_job(reminder_id)
                reminder.paused = False
                return True
            except Exception:
                return False

    async def update_reminder(
        self,
        reminder_id: str,
        content: str | None = None,
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
    ) -> Reminder | None:
        """更新提醒。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return None

            reminder = self._reminders[reminder_id]

            if content is not None:
                reminder.content = content

            if trigger_type is not None and trigger_config is not None:
                reminder.trigger_type = trigger_type
                reminder.trigger_config = trigger_config

                trigger = self._build_trigger(trigger_type, trigger_config)
                metadata_json = json.dumps(reminder.to_dict())
                try:
                    self._scheduler.remove_job(reminder_id)
                    self._scheduler.add_job(
                        _reminder_job_func,
                        trigger=trigger,
                        id=reminder_id,
                        args=[reminder_id, metadata_json],
                    )
                except Exception:
                    pass

            return reminder

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        """获取单个提醒。"""
        return self._reminders.get(reminder_id)

    def get_all_reminders(self, chat_id: int | None = None, active_only: bool = True) -> list[Reminder]:
        """获取所有提醒。
        
        Args:
            chat_id: 筛选指定用户的提醒
            active_only: 是否只返回有效的（未触发、未过期、未暂停）提醒
        """
        reminders = list(self._reminders.values())
        if chat_id is not None:
            reminders = [r for r in reminders if r.chat_id == chat_id]
        if active_only:
            reminders = [r for r in reminders if r.is_active()]
        return sorted(reminders, key=lambda r: r.created_at, reverse=True)

    def get_reminder_count(self, chat_id: int | None = None, active_only: bool = True) -> int:
        """获取提醒数量。"""
        return len(self.get_all_reminders(chat_id, active_only))

    def get_next_fire_times(self, reminder_id: str, count: int = 3) -> list[datetime]:
        """获取提醒接下来 N 次的触发时间。"""
        reminder = self._reminders.get(reminder_id)
        if not reminder:
            return []
        try:
            trigger = self._build_trigger(reminder.trigger_type, reminder.trigger_config)
        except Exception:
            return []
        times: list[datetime] = []
        prev: datetime | None = None
        # 用 aware datetime，和 APScheduler trigger 内部保持一致
        now = datetime.now().astimezone()
        for _ in range(count):
            try:
                next_time = trigger.get_next_fire_time(prev, now)
            except Exception:
                break
            if next_time is None:
                break
            # 去掉时区用于展示，但保留 aware 版本给下一轮用
            display_time = next_time.astimezone().replace(tzinfo=None) if next_time.tzinfo else next_time
            times.append(display_time)
            prev = next_time
            # now 必须推进到 prev 之后，否则下一次返回相同时间
            now = next_time + timedelta(seconds=1)
        return times

    def get_completed_reminders(self, chat_id: int | None = None) -> list[Reminder]:
        """获取已完成/已过期的提醒（按触发时间倒序）。"""
        reminders = list(self._reminders.values())
        if chat_id is not None:
            reminders = [r for r in reminders if r.chat_id == chat_id]
        reminders = [r for r in reminders if r.triggered or r.expired]
        return sorted(reminders, key=lambda r: r.triggered_at or r.created_at, reverse=True)

    async def mark_triggered(self, reminder_id: str) -> bool:
        """标记提醒为已触发。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False
            
            reminder = self._reminders[reminder_id]
            reminder.triggered = True
            reminder.triggered_at = datetime.now()
            
            # 从调度器中移除（但保留在内存记录中）
            try:
                self._scheduler.remove_job(reminder_id)
            except Exception:
                pass
            
            # 更新数据库中的状态
            await self._update_reminder_in_db(reminder)
            return True

    async def mark_expired(self, reminder_id: str) -> bool:
        """标记提醒为已过期。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False
            
            reminder = self._reminders[reminder_id]
            reminder.expired = True
            
            # 从调度器中移除
            try:
                self._scheduler.remove_job(reminder_id)
            except Exception:
                pass
            
            await self._update_reminder_in_db(reminder)
            return True

    async def update_reminder_info(self, reminder_id: str, info: str) -> bool:
        """更新提醒的备注信息。"""
        async with self._lock:
            if reminder_id not in self._reminders:
                return False
            
            reminder = self._reminders[reminder_id]
            reminder.info = info
            await self._save_reminder_record(reminder)
            return True

    async def _update_reminder_in_db(self, reminder: Reminder) -> None:
        """更新数据库中的提醒状态。"""
        if self._scheduler is None:
            return
        
        # 通过重新添加 job 来更新数据库
        metadata_json = json.dumps(reminder.to_dict())
        try:
            # 先移除旧的
            self._scheduler.remove_job(reminder.id)
        except Exception:
            pass
        
        # 添加一个已完成的占位 job（用于保留记录）
        # 注意：APScheduler 会自动清理已完成的 job
        # 我们需要另一种方式持久化状态