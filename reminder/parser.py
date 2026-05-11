"""提醒自然语言解析器。

所有解析都通过 Cursor CLI 完成，不使用硬编码正则。
直接通过 subprocess 调用 Cursor CLI，无需 iTerm2。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from module_config import ModuleConfig, load_modules_config


def _build_prompt(cli_path: str, python_path: str, chat_id: int, user_message: str) -> str:
    """构建发送给 Cursor Agent 的 Prompt。"""
    # 使用普通字符串拼接，避免 f-string 花括号问题
    return """你是一个提醒调度执行者。用户发送自然语言提醒请求，你必须**实际执行**创建提醒的操作，不能只给出建议或示例代码。

## 重要规则
1. **必须实际执行** `reminder_cli.py exec --async` 命令来创建提醒
2. **不能只输出建议** - 你有 shell 工具，必须真正运行命令
3. 时间计算必须在执行时进行（使用 `datetime.now()`），不能写死
4. 执行成功后，输出简洁的结果确认，包含 reminder_id 和触发时间

## 工具路径
reminder_cli.py: """ + cli_path + """
Python 解释器: """ + python_path + """
用户 Chat ID: """ + str(chat_id) + """

## 执行方式（必须按此格式执行）

对于一次性提醒（如"3分钟后提醒刷牙"），执行：
```bash
""" + python_path + """ """ + cli_path + """ exec --async --code "
from datetime import datetime, timedelta
run_time = datetime.now() + timedelta(minutes=3)
result = await scheduler.add_reminder(chat_id=""" + str(chat_id) + """, content='刷牙', trigger_type='date', trigger_config={'run_date': run_time.isoformat()})
print(json.dumps({'success': True, 'reminder_id': result.id, 'run_time': run_time.isoformat()}, ensure_ascii=False))
"
```

对于周期提醒（如"每周三上午9点提醒开会"），执行：
```bash
""" + python_path + """ """ + cli_path + """ exec --async --code "
result = await scheduler.add_reminder(chat_id=""" + str(chat_id) + """, content='开会', trigger_type='cron', trigger_config={'day_of_week': 'wed', 'hour': 9, 'minute': 0})
print(json.dumps({'success': True, 'reminder_id': result.id}, ensure_ascii=False))
"
```

对于复杂提醒（如"每月第二个周日提醒，排除1月"），执行：
```bash
""" + python_path + """ """ + cli_path + """ exec --async --code "
result = await scheduler.add_reminder(chat_id=""" + str(chat_id) + """, content='提醒内容', trigger_type='nth_weekday', trigger_config={'nth': 2, 'weekday': 6, 'hour': 10, 'minute': 0, 'exclude_months': [1]})
print(json.dumps({'success': True, 'reminder_id': result.id}, ensure_ascii=False))
"
```

## 用户请求
""" + user_message + """

## 你的任务
1. 解析用户意图
2. **立即执行**上述格式的命令（不要只输出代码，要真正运行）
3. 返回执行结果的 JSON

现在请执行创建提醒的操作。
"""


class ReminderParser:
    """提醒解析器 - 通过 Cursor CLI 解析自然语言。"""

    def __init__(
        self,
        reminder_cli_path: str | None = None,
        python_path: str | None = None,
        module_config: ModuleConfig | None = None,
    ):
        """
        初始化解析器。

        Args:
            reminder_cli_path: reminder_cli.py 的绝对路径（可选，默认自动推断）
            python_path: Python 解释器路径（可选，默认使用当前解释器）
            module_config: 模块配置（可选，默认从 modules.yaml 加载）
        """
        if reminder_cli_path is None:
            self._reminder_cli_path = str(Path(__file__).parent / "reminder_cli.py")
        else:
            self._reminder_cli_path = reminder_cli_path

        if python_path is None:
            self._python_path = sys.executable
        else:
            self._python_path = python_path

        # 加载模块配置
        if module_config is None:
            modules_config = load_modules_config()
            self._module_config = modules_config.get("reminder")
        else:
            self._module_config = module_config

        # 查找 cursor CLI 路径
        self._cursor_path = shutil.which("cursor")
        if not self._cursor_path:
            common_paths = [
                "/usr/local/bin/cursor",
                "/opt/homebrew/bin/cursor",
                str(Path.home() / ".cursor" / "bin" / "cursor"),
                "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
                str(Path.home() / "Applications" / "Cursor.app" / "Contents" / "Resources" / "app" / "bin" / "cursor"),
            ]
            for p in common_paths:
                if Path(p).exists():
                    self._cursor_path = str(p)
                    break

    async def parse_and_create(
        self,
        user_message: str,
        chat_id: int,
    ) -> dict[str, Any]:
        """
        通过 Cursor CLI 解析用户消息并创建提醒。

        直接调用 Cursor Agent CLI（后台静默执行）。

        Args:
            user_message: 用户的自然语言消息
            chat_id: Telegram Chat ID

        Returns:
            创建结果，包含 success、output 和可能的 error
        """
        prompt = _build_prompt(self._reminder_cli_path, self._python_path, chat_id, user_message)

        if not self._cursor_path:
            return {
                "success": False,
                "error": "未找到 cursor CLI，请确保已安装 Cursor",
            }

        # 构建命令参数
        args = [
            self._cursor_path,
            "agent",
            "--print",
            "--trust",
            "--yolo",
        ]

        # 添加模型参数（如果配置了非默认模型）
        if self._module_config.model and self._module_config.model != "auto":
            args.extend(["--model", self._module_config.model])

        args.append(prompt)

        timeout = self._module_config.timeout

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout),
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "output": output,
                }
            else:
                return {
                    "success": False,
                    "output": output,
                    "error": error,
                }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Cursor CLI 超时（{timeout}秒）",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    def get_cli_path(self) -> str:
        """获取 reminder_cli.py 的路径。"""
        return self._reminder_cli_path