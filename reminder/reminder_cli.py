#!/usr/bin/env python3
"""
提醒系统 CLI - 轻量级入口，供大模型调用。

设计原则：
1. CLI 只是一个轻量入口，不做任何业务逻辑
2. 大模型通过 CLI 获取数据库路径，然后自己连接数据库
3. 大模型可以自由执行任何 Python 代码
4. 大模型可以自定义调度策略、数据结构、查询逻辑
5. 提供 scheduler API，让 LLM 可以直接操作调度器

用法:
    # 获取系统信息（数据库路径、表结构、可用 API）
    python reminder_cli.py info

    # 执行任意 Python 代码（同步模式）
    python reminder_cli.py exec --code "..."

    # 执行异步代码（可访问 scheduler）
    python reminder_cli.py exec --code "await scheduler.add_reminder(...)" --async

示例:
    # 三分钟后提醒
    python reminder_cli.py exec --async --code "
from datetime import datetime, timedelta
run_time = datetime.now() + timedelta(minutes=3)
result = await scheduler.add_reminder(
    chat_id=123456,
    content='刷牙',
    trigger_type='date',
    trigger_config={'run_date': run_time.isoformat()}
)
print(json.dumps({'success': True, 'reminder_id': result.id}))
"

    # 每月第二个周日提醒（排除1月）
    python reminder_cli.py exec --async --code "
result = await scheduler.add_reminder(
    chat_id=123456,
    content='开会',
    trigger_type='nth_weekday',
    trigger_config={'nth': 2, 'weekday': 6, 'hour': 10, 'minute': 0, 'exclude_months': [1]}
)
print(json.dumps({'success': True, 'reminder': result.to_dict()}))
"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from typing import Any

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from reminder.manager import ReminderManager
from reminder.models import Reminder

DB_PATH = Path.home() / ".tg2iterm2" / "reminders.db"


def get_db_schema() -> dict:
    """
    获取数据库表结构信息。

    Returns:
        包含所有表及其字段信息的字典
    """
    db_path = str(DB_PATH)
    if not Path(db_path).exists():
        return {}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    schema = {}

    # 获取所有表名
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    # 获取每个表的结构
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = []
        for row in cursor.fetchall():
            columns.append({
                "name": row[1],
                "type": row[2],
                "notnull": bool(row[3]),
                "default": row[4],
                "pk": bool(row[5]),
            })
        schema[table] = columns

    conn.close()
    return schema


def get_sample_data(table_name: str, limit: int = 3) -> list[dict]:
    """
    获取表的示例数据。

    Args:
        table_name: 表名
        limit: 返回的记录数上限

    Returns:
        示例数据列表
    """
    db_path = str(DB_PATH)
    if not Path(db_path).exists():
        return []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute(f"SELECT * FROM {table_name} LIMIT {limit}")
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        result = []
        for row in rows:
            result.append(dict(zip(columns, row)))
        return result
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def cmd_info(args) -> None:
    """
    返回系统信息，包括数据库路径、表结构、可用 API。

    大模型可以根据这些信息自行决定如何操作数据库。
    """
    # 获取表结构
    schema = get_db_schema()

    # 获取每个表的示例数据
    sample_data = {}
    for table in schema.keys():
        sample_data[table] = get_sample_data(table)

    # 构建返回信息
    info = {
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "tables": list(schema.keys()),
        "schema": schema,
        "sample_data": sample_data,
        "apis": {
            "sqlite3": "Python 标准库，可直接连接数据库",
            "apscheduler": "已安装，可用于调度任务（from apscheduler.schedulers.asyncio import AsyncIOScheduler）",
            "scheduler": "ReminderManager 实例，在 async 模式下可用",
        },
        "scheduler_api": {
            "add_reminder": {
                "description": "添加新提醒",
                "signature": "await scheduler.add_reminder(chat_id, content, trigger_type, trigger_config)",
                "trigger_types": ["date", "cron", "nth_weekday"],
                "trigger_config_examples": {
                    "date": {"run_date": "2026-05-10T18:00:00", "timezone": "Asia/Shanghai"},
                    "cron": {"hour": 9, "minute": 0, "day_of_week": "mon-fri"},
                    "nth_weekday": {"nth": 2, "weekday": 6, "hour": 10, "minute": 0, "exclude_months": [1]},
                },
            },
            "remove_reminder": {
                "description": "删除提醒",
                "signature": "await scheduler.remove_reminder(reminder_id)",
            },
            "pause_reminder": {
                "description": "暂停提醒",
                "signature": "await scheduler.pause_reminder(reminder_id)",
            },
            "resume_reminder": {
                "description": "恢复提醒",
                "signature": "await scheduler.resume_reminder(reminder_id)",
            },
            "get_reminder": {
                "description": "获取单个提醒",
                "signature": "scheduler.get_reminder(reminder_id)",
            },
            "get_all_reminders": {
                "description": "获取所有提醒",
                "signature": "scheduler.get_all_reminders(chat_id=None)",
            },
        },
        "usage_examples": {
            "connect_db": f"import sqlite3; conn = sqlite3.connect('{DB_PATH}')",
            "query_jobs": "cursor = conn.cursor(); cursor.execute('SELECT * FROM apscheduler_jobs')",
            "add_reminder_async": "python reminder_cli.py exec --async --code 'await scheduler.add_reminder(chat_id=123, content=\"test\", trigger_type=\"date\", trigger_config={\"run_date\": \"2026-05-10T18:00:00\"})'",
        },
    }

    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))


def cmd_exec(args) -> None:
    """
    执行任意 Python 代码。

    大模型可以自由编写代码来操作数据库、实现自定义逻辑等。
    代码在独立的命名空间中执行，可以访问常用模块。
    
    如果指定 --async 标志，代码在异步环境中执行，
    可以使用 await 调用 scheduler 的异步方法。
    """
    code = args.code
    is_async = getattr(args, 'async_mode', False)

    # 准备基础执行环境
    import datetime as dt_module
    
    base_globals = {
        "__builtins__": __builtins__,
        "json": json,
        "sqlite3": sqlite3,
        "Path": Path,
        "DB_PATH": DB_PATH,
        "datetime": dt_module.datetime,
        "timedelta": dt_module.timedelta,
    }

    if is_async:
        # 异步模式：提供 scheduler 实例
        async def _run_async():
            manager = ReminderManager(db_path=DB_PATH)
            await manager.start()
            try:
                # 构建执行环境
                local_vars = dict(base_globals)
                local_vars["scheduler"] = manager
                local_vars["Reminder"] = Reminder
                
                # 创建异步执行函数
                exec_globals = {"__builtins__": __builtins__}
                func_code = f"""
import json
from datetime import datetime, timedelta

async def __async_exec(scheduler, Reminder, json, datetime, timedelta):
{chr(10).join('    ' + line if line.strip() else '' for line in code.split(chr(10)))}
"""
                try:
                    exec(func_code, exec_globals, local_vars)
                    await local_vars["__async_exec"](manager, Reminder, json, dt_module.datetime, dt_module.timedelta)
                except Exception as e:
                    result = {
                        "success": False,
                        "error": f"{type(e).__name__}: {e}",
                    }
                    print(json.dumps(result, ensure_ascii=False))
            finally:
                await manager.stop()
        
        asyncio.run(_run_async())
    else:
        # 同步模式：直接执行
        try:
            exec(code, base_globals)
        except Exception as e:
            result = {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
            }
            print(json.dumps(result, ensure_ascii=False))


def cmd_shell(args) -> None:
    """
    进入交互式 Python 环境（可选）。

    提供一个预配置的 Python REPL，已导入常用模块和数据库路径。
    """
    import code

    print("进入交互式 Python 环境")
    print(f"数据库路径: {DB_PATH}")
    print("已导入: json, sqlite3, Path, DB_PATH")
    print("输入 exit() 或 Ctrl+D 退出")
    print("-" * 40)

    # 准备交互环境
    local_vars = {
        "json": json,
        "sqlite3": sqlite3,
        "Path": Path,
        "DB_PATH": DB_PATH,
    }

    # 启动 REPL
    code.interact(local=local_vars)


def cmd_query(args) -> None:
    """
    执行 SQL 查询（便捷命令）。

    大模型可以直接执行 SQL 查询，无需编写完整的 Python 代码。
    """
    sql = args.sql
    params = json.loads(args.params) if args.params else []

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        cursor.execute(sql, params)

        # 判断是查询还是修改
        if sql.strip().upper().startswith(("SELECT", "PRAGMA")):
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            result = {
                "success": True,
                "columns": columns,
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
            }
        else:
            conn.commit()
            result = {
                "success": True,
                "affected_rows": cursor.rowcount,
            }

        conn.close()
        print(json.dumps(result, ensure_ascii=False, default=str))

    except Exception as e:
        result = {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
        }
        print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="提醒系统 CLI - 轻量级入口，供大模型调用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 获取系统信息
  python reminder_cli.py info

  # 执行 SQL 查询
  python reminder_cli.py query --sql "SELECT * FROM apscheduler_jobs"

  # 执行任意 Python 代码
  python reminder_cli.py exec --code "
import sqlite3
conn = sqlite3.connect(str(DB_PATH))
cursor = conn.cursor()
cursor.execute('SELECT * FROM apscheduler_jobs')
print(json.dumps(cursor.fetchall(), default=str))
conn.close()
"
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # info 命令
    info_parser = subparsers.add_parser(
        "info",
        help="获取系统信息（数据库路径、表结构、可用 API）",
    )
    info_parser.set_defaults(func=cmd_info)

    # exec 命令
    exec_parser = subparsers.add_parser(
        "exec",
        help="执行任意 Python 代码",
    )
    exec_parser.add_argument(
        "--code",
        type=str,
        required=True,
        help="要执行的 Python 代码",
    )
    exec_parser.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="异步执行模式，可使用 await 调用 scheduler API",
    )
    exec_parser.set_defaults(func=cmd_exec)

    # shell 命令
    shell_parser = subparsers.add_parser(
        "shell",
        help="进入交互式 Python 环境",
    )
    shell_parser.set_defaults(func=cmd_shell)

    # query 命令
    query_parser = subparsers.add_parser(
        "query",
        help="执行 SQL 查询",
    )
    query_parser.add_argument(
        "--sql",
        type=str,
        required=True,
        help="SQL 查询语句",
    )
    query_parser.add_argument(
        "--params",
        type=str,
        help="SQL 参数（JSON 数组）",
    )
    query_parser.set_defaults(func=cmd_query)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
