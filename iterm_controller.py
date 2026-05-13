"""iTerm2 Python API 控制封装。"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import iterm2


ENTER_KEY = "\r"
SCREEN_TEXT_PROMPT_ID = "__screen_text__"
CLAUDE_DONE_SIGNAL_DEFAULT = Path("/tmp/tg2iterm2_claude_done")
CLAUDE_STATUS_RE = re.compile(r"^\s*✻\s+.+\s+for\s+\d+(?:\.\d+)?s\s*$")
CLAUDE_TIP_RE = re.compile(r"^\s*(?:⎿\s*)?tip:\s+.+$", re.IGNORECASE)
CLAUDE_SPINNER_RE = re.compile(r"^\s*[✽✻]\s+.+(?:\.{3}|…)\s*(?:\(.*\))?\s*$")
PASTED_INPUT_PREFIX_RE = re.compile(r"^\[Pasted\b.*", re.IGNORECASE)
CURSOR_FOLLOW_UP_RE = re.compile(r"^→\s+Add a follow-up\s*$", re.IGNORECASE)
CURSOR_COMPOSER_STATUS_RE = re.compile(r"^Composer\b.*$", re.IGNORECASE)
OPENCODE_TOKENS_RE = re.compile(r"^[0-9][0-9,\.]*\s+tokens$", re.IGNORECASE)
OPENCODE_PERCENT_RE = re.compile(r"^[0-9][0-9,\.]*%\s+used$", re.IGNORECASE)
OPENCODE_COST_RE = re.compile(r"^\$[0-9][0-9,\.]*\s+spent$", re.IGNORECASE)
OPENCODE_COMMANDS_RE = re.compile(r"^.*ctrl\+p\s+commands.*OpenCode.*$", re.IGNORECASE)

INTERACTIVE_CLI_NAMES = {"claude", "agent", "opencode"}


@dataclass(frozen=True)
class ScreenSnapshot:
    """保存屏幕文本和当前光标所在行。"""

    text: str
    cursor_line: str
    cursor_x: int


@dataclass(frozen=True)
class CommandResult:
    """保存终端命令执行结果。"""

    exit_status: int | None
    output: str


class ITermController:
    """封装 iTerm2 连接、tab 选择和终端输入。"""

    def __init__(
        self,
        default_tab_number: int | None = None,
        claude_done_signal: str | None = None,
        claude_hook_timeout: float = 300.0,
        cursor_done_signal: str | None = None,
        cursor_hook_timeout: float = 300.0,
    ) -> None:
        """初始化 iTerm2 控制器。"""
        self._connection: iterm2.Connection | None = None
        self._default_tab_number = default_tab_number
        self._default_tab_unique_id: str | None = None
        self._foreground_session: iterm2.Session | None = None
        self._foreground_prompt_id: str | None = None
        self._foreground_command_name: str | None = None
        self._foreground_last_output = ""
        self._foreground_stdin_lock = asyncio.Lock()
        self._suppress_foreground_stream = False
        self._claude_done_signal = Path(claude_done_signal) if claude_done_signal else CLAUDE_DONE_SIGNAL_DEFAULT
        self._claude_hook_timeout = claude_hook_timeout
        self._cursor_done_signal = Path(cursor_done_signal) if cursor_done_signal else Path("/tmp/tg2iterm2_cursor_done")
        self._cursor_hook_timeout = cursor_hook_timeout

    async def connect(self) -> None:
        """连接 iTerm2；首次失败时尝试启动 iTerm2 后重试。"""
        if sys.platform != "darwin":
            raise RuntimeError("tg2iterm2 仅支持 macOS")
        if self._connection is not None:
            return
        try:
            self._connection = await iterm2.Connection.async_create()
        except Exception:
            await self.launch_iterm2()
            await asyncio.sleep(2)
            self._connection = await iterm2.Connection.async_create()
        await self._capture_initial_active_tab()

    async def launch_iterm2(self) -> None:
        """通过 macOS open 命令启动 iTerm2。"""
        for app_name in ("iTerm", "iTerm2"):
            process = await asyncio.create_subprocess_exec(
                "open",
                "-a",
                app_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode == 0:
                return
        raise RuntimeError("无法启动 iTerm2")

    async def _get_app(self) -> iterm2.App:
        """获取最新的 iTerm2 App 对象。"""
        await self.connect()
        assert self._connection is not None
        app = await iterm2.async_get_app(self._connection)
        if app is None:
            raise RuntimeError("无法获取 iTerm2 App")
        await app.async_refresh_focus()
        return app

    async def list_tabs_text(self) -> str:
        """按当前可见顺序列出 tab 编号。"""
        app = await self._get_app()
        lines: list[str] = []
        for number, window, tab in self._ordered_tabs(app):
            markers = []
            if window.current_tab == tab:
                markers.append("当前")
            if self._is_default_tab(number, tab):
                markers.append("默认")
            title = self._safe_title(tab.current_session) or "(无标题)"
            marker_text = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"{number}. {title}{marker_text}")
        return "\n".join(lines) if lines else "当前窗口没有 tab"

    async def set_default_tab(self, tab_number: str) -> int:
        """按当前可见顺序编号切换默认 tab，并激活该 tab。"""
        number = parse_tab_number(tab_number)
        tab = await self._find_tab_by_number(number)
        if tab is None:
            raise RuntimeError(f"找不到编号为 {number} 的 tab")
        await tab.async_activate()
        self._default_tab_number = number
        self._default_tab_unique_id = str(tab.tab_id)
        return number

    async def create_new_tab(self, activate: bool = True) -> int:
        """在当前窗口新建 tab；可选择是否保持当前可见 tab 不变。"""
        app = await self._get_app()
        window = app.current_window
        if window is None:
            assert self._connection is not None
            window = await iterm2.Window.async_create(self._connection)
            if window is None:
                raise RuntimeError("创建 iTerm2 窗口失败")
            app = await self._get_app()
            window = app.current_window or window
        previous_tab = window.current_tab
        tab = await window.async_create_tab()
        if tab is None:
            raise RuntimeError("创建 iTerm2 tab 失败")
        self._default_tab_unique_id = str(tab.tab_id)
        self._default_tab_number = await self._number_for_tab(tab)
        if activate:
            await tab.async_activate()
        elif previous_tab is not None and previous_tab.tab_id != tab.tab_id:
            await previous_tab.async_activate()
        return self._default_tab_number

    def clear_default_tab(self) -> None:
        """清除当前绑定的默认目标 tab。"""
        self._default_tab_unique_id = None
        self._default_tab_number = None

    async def close_target_tab(self) -> None:
        """关闭当前绑定的目标 tab，并清理前台状态与默认绑定。"""
        tab: iterm2.Tab | None = None
        if self._default_tab_unique_id:
            tab = await self._find_tab_by_unique_id(self._default_tab_unique_id)
        elif self._default_tab_number:
            tab = await self._find_tab_by_number(self._default_tab_number)
        self.clear_default_tab()
        if tab is None:
            self._foreground_session = None
            self._foreground_prompt_id = None
            self._foreground_command_name = None
            self._foreground_last_output = ""
            self._suppress_foreground_stream = False
            return
        session = tab.current_session
        if session is not None:
            self._clear_foreground_state(session)
        else:
            self._foreground_session = None
            self._foreground_prompt_id = None
            self._foreground_command_name = None
            self._foreground_last_output = ""
            self._suppress_foreground_stream = False
        await tab.async_close(force=True)

    async def send_text(self, text: str, enter: bool = False) -> None:
        """向当前 session 输入文本，可选择是否追加回车。"""
        session = await self.get_target_session()
        suffix = ENTER_KEY if enter else ""
        await session.async_send_text(text + suffix, suppress_broadcast=True)

    async def send_ctrl_c(self) -> None:
        """向当前 session 发送 Ctrl+C。"""
        await self.send_control_character("\x03")

    async def send_ctrl_d(self) -> None:
        """向当前 session 发送 Ctrl+D。"""
        await self.send_control_character("\x04")

    async def send_enter(self) -> None:
        """向当前 session 只发送回车键。"""
        await self.send_control_character(ENTER_KEY)

    async def send_control_character(self, character: str) -> None:
        """发送控制字符。"""
        session = await self.get_target_session()
        await session.async_send_text(character, suppress_broadcast=True)

    async def get_target_session(self) -> iterm2.Session:
        """根据默认编号或当前 tab 获取目标 session。"""
        tab = None
        if self._default_tab_unique_id:
            tab = await self._find_tab_by_unique_id(self._default_tab_unique_id)
            if tab is None:
                raise RuntimeError("默认 tab 已不存在")
        elif self._default_tab_number:
            tab = await self._find_tab_by_number(self._default_tab_number)
            if tab is None:
                raise RuntimeError(f"默认 tab 编号不存在: {self._default_tab_number}")
            self._default_tab_unique_id = str(tab.tab_id)
        if tab is None:
            tab = await self._current_tab_or_create()
        session = tab.current_session
        if session is None:
            raise RuntimeError("目标 tab 没有活跃 session")
        return session

    async def read_last_lines(self, count: int) -> str:
        """读取当前 session 屏幕文本的倒数 N 行。"""
        session = await self.get_target_session()
        text = await self.read_session_screen_text(session)
        lines = text.splitlines()
        return "\n".join(lines[-count:]) if lines else ""

    async def wait_until_shell_ready(self, timeout: float = 30.0) -> None:
        """等待当前目标 tab 中的 shell prompt 就绪。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_text = ""
        while loop.time() < deadline:
            last_text = await self.read_last_lines(10)
            if any(_is_shell_prompt_line(line) for line in last_text.splitlines()):
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"等待 shell prompt 超时，最后屏幕内容: {last_text!r}")

    async def read_session_screen_text(self, session: iterm2.Session) -> str:
        """读取指定 session 当前屏幕内容。"""
        snapshot = await self._read_session_screen_snapshot(session)
        return snapshot.text

    async def run_command_stream(
        self,
        command: str,
        on_update: Any,
        stream_interval: float,
    ) -> CommandResult:
        """执行命令并通过回调流式返回屏幕变化。"""
        session = await self.get_target_session()
        assert self._connection is not None
        self._set_foreground_state(
            session=session,
            prompt_id=None,
            command_name=command_name(command),
        )
        prompt_modes = [
            iterm2.PromptMonitor.Mode.COMMAND_START,
            iterm2.PromptMonitor.Mode.COMMAND_END,
        ]
        async with session.get_screen_streamer() as streamer:
            async with iterm2.PromptMonitor(
                self._connection,
                session.session_id,
                modes=prompt_modes,
            ) as prompt_monitor:
                await session.async_send_text(
                    command + ENTER_KEY,
                    suppress_broadcast=True,
                )
                result = await self._wait_command_end(
                    session=session,
                    streamer=streamer,
                    prompt_monitor=prompt_monitor,
                    on_update=on_update,
                    stream_interval=stream_interval,
                )
        return result

    async def _wait_command_end(
        self,
        session: iterm2.Session,
        streamer: iterm2.ScreenStreamer,
        prompt_monitor: iterm2.PromptMonitor,
        on_update: Any,
        stream_interval: float,
    ) -> CommandResult:
        """等待命令结束，并按节流间隔推送屏幕更新。"""
        exit_status: int | None = None
        prompt_id: str | None = None
        last_sent_at = 0.0
        screen_task = asyncio.create_task(streamer.async_get())
        prompt_task = asyncio.create_task(prompt_monitor.async_get(include_id=True))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {screen_task, prompt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if prompt_task in done:
                    mode, value, event_prompt_id = prompt_task.result()
                    if mode == iterm2.PromptMonitor.Mode.COMMAND_START:
                        prompt_id = event_prompt_id
                        self._set_foreground_state(
                            session=session,
                            prompt_id=prompt_id,
                            command_name=command_name(str(value or command)),
                        )
                        prompt_task = asyncio.create_task(
                            prompt_monitor.async_get(include_id=True)
                        )
                        continue
                    if mode == iterm2.PromptMonitor.Mode.COMMAND_END:
                        prompt_id = prompt_id or event_prompt_id
                        exit_status = int(value) if value is not None else None
                        break
                if screen_task in done:
                    screen_task.result()
                    if prompt_id is None or self._suppress_foreground_stream:
                        screen_task = asyncio.create_task(streamer.async_get())
                        continue
                    last_output = await self._read_current_command_output(
                        session=session,
                        prompt_id=prompt_id,
                    )
                    now = asyncio.get_running_loop().time()
                    if now - last_sent_at >= stream_interval:
                        await on_update(last_output)
                        last_sent_at = now
                    screen_task = asyncio.create_task(streamer.async_get())
        finally:
            for task in (screen_task, prompt_task):
                if not task.done():
                    task.cancel()
        if self._suppress_foreground_stream:
            final_output = ""
        else:
            final_output = await self._read_current_command_output_after_settle(
                session=session,
                prompt_id=prompt_id,
            )
        self._clear_foreground_state(session)
        return CommandResult(exit_status=exit_status, output=final_output)

    async def send_foreground_input_stream(
        self,
        text: str,
        on_update: Any,
        stream_interval: float,
        idle_seconds: float = 1.0,
    ) -> str:
        """向未结束的前台命令发送 stdin，并返回本次输入后的新增输出。"""
        async with self._foreground_stdin_lock:
            session, prompt_id = await self._wait_foreground_prompt_id()
            if session is None:
                session = await self.get_target_session()

            self._suppress_foreground_stream = True
            before_output = await self._read_current_command_output_after_settle(
                session=session,
                prompt_id=prompt_id,
            )
            if (
                self._foreground_last_output
                and len(self._foreground_last_output) > len(before_output)
            ):
                before_output = self._foreground_last_output
            before_ns = time.time_ns()

            is_interactive_cli = self._foreground_command_name in INTERACTIVE_CLI_NAMES
            if self._foreground_command_name == "agent":
                # Cursor Agent 的输入框对粘贴内容更敏感，双回车更稳定地触发提交。
                await session.async_send_text(text, suppress_broadcast=True)
                await asyncio.sleep(0.15)
                await session.async_send_text(ENTER_KEY, suppress_broadcast=True)
                await asyncio.sleep(0.15)
                await session.async_send_text(ENTER_KEY, suppress_broadcast=True)
            elif is_interactive_cli and self._foreground_command_name != "claude":
                # OpenCode 等非 Claude CLI 需要分步发送文本和回车
                await session.async_send_text(text, suppress_broadcast=True)
                await asyncio.sleep(0.15)
                await session.async_send_text(ENTER_KEY, suppress_broadcast=True)
            else:
                await session.async_send_text(text + ENTER_KEY, suppress_broadcast=True)

            if prompt_id is None:
                return ""
            return await self._collect_foreground_delta(
                session=session,
                prompt_id=prompt_id,
                before_output=before_output,
                submitted_text=text,
                is_interactive_cli=is_interactive_cli,
                on_update=on_update,
                stream_interval=stream_interval,
                idle_seconds=idle_seconds,
                before_ns=before_ns,
            )

    async def _wait_foreground_prompt_id(
        self,
        timeout: float = 2.0,
    ) -> tuple[iterm2.Session | None, str | None]:
        """等待前台命令的 prompt_id 出现。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._foreground_session is not None and self._foreground_prompt_id is None:
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)
        return self._foreground_session, self._foreground_prompt_id

    def _read_hook_signal_ns(self, cli_name: str | None = None) -> int | None:
        """读取 hook 信号文件中的纳秒时间戳，失败返回 None。

        Args:
            cli_name: CLI 名称，用于选择对应的信号文件。默认读取 Claude 信号。
        """
        if cli_name == "agent":
            signal_path = self._cursor_done_signal
        else:
            signal_path = self._claude_done_signal
        try:
            content = signal_path.read_text().strip()
            return int(content)
        except (OSError, ValueError):
            return None

    async def _collect_foreground_delta(
        self,
        session: iterm2.Session,
        prompt_id: str,
        before_output: str,
        submitted_text: str,
        is_interactive_cli: bool,
        on_update: Any,
        stream_interval: float,
        idle_seconds: float,
        before_ns: int = 0,
    ) -> str:
        """读取前台命令 output_range 中本次 stdin 之后新增的内容。"""
        loop = asyncio.get_running_loop()
        last_change_at = loop.time()
        last_sent_at = 0.0
        last_delta = ""
        seen_input_anchor = False
        cli_name = self._foreground_command_name
        hook_timeout = (
            self._cursor_hook_timeout if cli_name == "agent"
            else self._claude_hook_timeout
        )
        hook_deadline = loop.time() + hook_timeout if is_interactive_cli else 0.0
        while True:
            await asyncio.sleep(min(max(stream_interval, 0.1), 0.5))
            current_output = await self._read_current_command_output(
                session=session,
                prompt_id=prompt_id,
            )
            if find_last_anchor(current_output, submitted_text) >= 0:
                seen_input_anchor = True
            elif _find_loose_anchor(current_output, submitted_text) >= 0:
                seen_input_anchor = True
            delta = output_after(before_output, current_output, submitted_text)
            now = loop.time()
            delta_changed = delta != last_delta
            if delta != last_delta:
                last_delta = delta
                last_change_at = now
            if is_interactive_cli:
                signal_ns = self._read_hook_signal_ns(cli_name)
                cursor_has_answer = _has_cursor_answer(last_delta) if cli_name == "agent" else False
                completed = (
                    signal_ns is not None
                    and signal_ns > before_ns
                )
                if not completed and seen_input_anchor:
                    if cli_name == "claude":
                        if has_claude_answer(last_delta) and has_claude_ready_prompt_tail(current_output):
                            completed = True
                    elif cli_name == "opencode":
                        if _has_opencode_answer(last_delta) and now - last_change_at >= idle_seconds:
                            completed = True
                    else:
                        if _has_cursor_ready_state(current_output) and cursor_has_answer:
                            completed = True
                if not completed and cli_name == "agent":
                    if _has_cursor_ready_state(current_output) and cursor_has_answer:
                        completed = True
                if not completed and cli_name == "opencode":
                    if _has_opencode_answer(last_delta) and now - last_change_at >= idle_seconds:
                        completed = True
                if not completed and now > hook_deadline:
                    completed = seen_input_anchor
                if completed and cli_name == "claude":
                    rendered_delta = clean_claude_delta(last_delta)
                elif completed and cli_name == "opencode":
                    rendered_delta = _clean_opencode_delta(last_delta)
                elif completed:
                    rendered_delta = _clean_generic_delta(last_delta)
                else:
                    rendered_delta = last_delta
            else:
                completed = False
                rendered_delta = last_delta
            should_update = delta_changed and now - last_sent_at >= stream_interval
            if completed:
                final_output = await self._read_current_command_output_after_settle(
                    session=session,
                    prompt_id=prompt_id,
                )
                if len(current_output) > len(final_output):
                    final_output = current_output
                if len(self._foreground_last_output) > len(final_output):
                    final_output = self._foreground_last_output
                self._foreground_last_output = final_output or current_output
                final_delta = output_after(
                    before_output,
                    self._foreground_last_output,
                    submitted_text,
                )
                if not final_delta or (
                    cli_name == "claude" and not has_claude_answer(final_delta)
                ):
                    final_delta = last_delta
                if cli_name == "claude":
                    final_rendered_delta = clean_claude_delta(final_delta)
                elif cli_name == "opencode":
                    final_rendered_delta = _clean_opencode_delta(final_delta)
                else:
                    final_rendered_delta = _clean_generic_delta(final_delta)
                if cli_name == "agent" and not _has_cursor_answer(final_delta):
                    continue
                if cli_name == "opencode" and not _has_opencode_answer(final_delta):
                    continue
                await on_update(final_rendered_delta)
                return final_rendered_delta
            if should_update:
                await on_update(rendered_delta)
                last_sent_at = now
            if not is_interactive_cli and now - last_change_at >= idle_seconds:
                self._foreground_last_output = current_output
                return last_delta

    def _set_foreground_state(
        self,
        session: iterm2.Session,
        prompt_id: str | None,
        command_name: str | None,
    ) -> None:
        """记录当前未结束的前台命令状态。"""
        self._foreground_session = session
        if prompt_id is not None and prompt_id != self._foreground_prompt_id:
            self._foreground_last_output = ""
        self._foreground_prompt_id = prompt_id
        if command_name is not None:
            self._foreground_command_name = command_name
        if prompt_id is None:
            self._suppress_foreground_stream = False

    def _clear_foreground_state(self, session: iterm2.Session) -> None:
        """清理已结束的前台命令状态。"""
        if self._foreground_session is session:
            self._foreground_session = None
            self._foreground_prompt_id = None
            self._foreground_command_name = None
            self._foreground_last_output = ""
            self._suppress_foreground_stream = False

    async def _capture_initial_active_tab(self) -> None:
        """首次连接时锁定 iTerm2 当前活跃 tab，而不是默认第一个 tab。"""
        if self._default_tab_unique_id is not None:
            return
        assert self._connection is not None
        app = await iterm2.async_get_app(self._connection)
        if app is None:
            return
        await app.async_refresh_focus()
        if self._default_tab_number is not None:
            tab = self._tab_by_number_from_app(app, self._default_tab_number)
        else:
            window = app.current_window
            tab = window.current_tab if window is not None else None
            if tab is not None:
                self._default_tab_number = self._number_for_tab_from_app(app, tab)
        if tab is not None:
            self._default_tab_unique_id = str(tab.tab_id)

    async def _read_current_command_output(
        self,
        session: iterm2.Session,
        prompt_id: str | None,
    ) -> str:
        """只按 shell integration 的 prompt 输出范围读取本次命令输出。"""
        if prompt_id == SCREEN_TEXT_PROMPT_ID:
            return await self.read_session_screen_text(session)
        if prompt_id:
            try:
                output = await self._read_prompt_output(session, prompt_id)
                if output is not None:
                    return output
            except Exception:
                pass
        return ""

    async def _read_current_command_output_after_settle(
        self,
        session: iterm2.Session,
        prompt_id: str | None,
    ) -> str:
        """命令刚结束时等待同一 output_range 稳定后再读取。"""
        loop = asyncio.get_running_loop()
        best_output = ""
        stable_reads = 0
        last_output: str | None = None
        first_output_at: float | None = None
        deadline = loop.time() + 2.4
        while loop.time() < deadline:
            output = await self._read_current_command_output(session, prompt_id)
            now = loop.time()
            if output and first_output_at is None:
                first_output_at = now
            if output and output != best_output:
                if len(output) >= len(best_output):
                    best_output = output
                stable_reads = 0
            elif output and output == last_output:
                stable_reads += 1

            if output != last_output:
                last_output = output
                stable_reads = 0

            has_observed_enough = (
                first_output_at is not None and now - first_output_at >= 0.9
            )
            if best_output and has_observed_enough and stable_reads >= 3:
                return best_output
            await asyncio.sleep(0.15)
        return best_output

    async def _read_session_screen_snapshot(
        self,
        session: iterm2.Session,
    ) -> ScreenSnapshot:
        """读取当前屏幕文本，并定位光标所在行。"""
        screen = await session.async_get_screen_contents()
        lines = screen_to_lines(screen)
        cursor = screen.cursor_coord
        row = cursor.y
        start_y = screen.windowed_coord_range.coordRange.start.y
        if not 0 <= row < len(lines):
            row = cursor.y - start_y
        cursor_line = lines[row] if 0 <= row < len(lines) else ""
        return ScreenSnapshot(
            text=lines_to_text(lines),
            cursor_line=cursor_line,
            cursor_x=cursor.x,
        )

    async def _read_prompt_output(
        self,
        session: iterm2.Session,
        prompt_id: str,
    ) -> str | None:
        """通过 prompt_id 精确读取当前命令对应的 output_range。"""
        assert self._connection is not None
        prompt = await iterm2.async_get_prompt_by_id(
            self._connection,
            session.session_id,
            prompt_id,
        )
        if prompt is None:
            return None
        coord_range = prompt.output_range
        if coord_range.start == coord_range.end:
            return ""
        windowed_range = iterm2.WindowedCoordRange(coord_range)
        response = await iterm2.rpc.async_get_screen_contents(
            connection=self._connection,
            session=session.session_id,
            windowed_coord_range=windowed_range,
            style=False,
        )
        if (
            response.get_buffer_response.status
            != iterm2.api_pb2.GetBufferResponse.Status.Value("OK")
        ):
            return None
        return screen_to_text(iterm2.ScreenContents(response.get_buffer_response))

    async def _current_tab_or_create(self) -> iterm2.Tab:
        """获取当前 tab，不存在时创建一个。"""
        app = await self._get_app()
        window = app.current_window
        if window is None:
            assert self._connection is not None
            window = await iterm2.Window.async_create(self._connection)
            if window is None:
                raise RuntimeError("创建 iTerm2 窗口失败")
            app = await self._get_app()
            window = app.current_window or window
        tab = window.current_tab
        if tab is None:
            tab = await window.async_create_tab()
            if tab is None:
                raise RuntimeError("创建 iTerm2 tab 失败")
        return tab

    async def _find_tab_by_number(self, tab_number: int) -> iterm2.Tab | None:
        """按当前可见顺序编号查找 tab。"""
        app = await self._get_app()
        return self._tab_by_number_from_app(app, tab_number)

    async def _find_tab_by_unique_id(self, unique_id: str) -> iterm2.Tab | None:
        """按内部唯一标识查找 tab，不暴露给 Telegram 用户。"""
        app = await self._get_app()
        for window in app.terminal_windows:
            for tab in window.tabs:
                if str(tab.tab_id) == unique_id:
                    return tab
        return None

    async def _number_for_tab(self, target_tab: iterm2.Tab) -> int:
        """按当前可见顺序获取指定 tab 的编号。"""
        app = await self._get_app()
        number = self._number_for_tab_from_app(app, target_tab)
        if number is not None:
            return number
        raise RuntimeError("新建 tab 后未能定位当前编号")

    def _number_for_tab_from_app(
        self,
        app: iterm2.App,
        target_tab: iterm2.Tab,
    ) -> int | None:
        """从给定 App 快照中获取 tab 编号。"""
        for number, _window, tab in self._ordered_tabs(app):
            if tab.tab_id == target_tab.tab_id:
                return number
        return None

    def _tab_by_number_from_app(
        self,
        app: iterm2.App,
        tab_number: int,
    ) -> iterm2.Tab | None:
        """从给定 App 快照中按编号获取 tab。"""
        for number, _window, tab in self._ordered_tabs(app):
            if number == tab_number:
                return tab
        return None

    def _ordered_tabs(
        self,
        app: iterm2.App,
    ) -> list[tuple[int, iterm2.Window, iterm2.Tab]]:
        """按 iTerm2 当前窗口的 tab 顺序生成用户可见编号。"""
        ordered: list[tuple[int, iterm2.Window, iterm2.Tab]] = []
        window = app.current_window
        if window is None:
            return ordered
        number = 1
        for tab in window.tabs:
            ordered.append((number, window, tab))
            number += 1
        return ordered

    def _is_default_tab(self, number: int, tab: iterm2.Tab) -> bool:
        """判断 tab 是否是默认目标。"""
        if self._default_tab_unique_id:
            return str(tab.tab_id) == self._default_tab_unique_id
        return number == self._default_tab_number

    def _safe_title(self, session: iterm2.Session | None) -> str:
        """安全获取 session 标题。"""
        if session is None:
            return ""
        return getattr(session, "name", "") or getattr(session, "title", "") or ""


def screen_to_text(screen: iterm2.ScreenContents) -> str:
    """将 iTerm2 ScreenContents 转成普通文本。"""
    return lines_to_text(screen_to_lines(screen))


def screen_to_lines(screen: iterm2.ScreenContents) -> list[str]:
    """将 iTerm2 ScreenContents 转成行列表。"""
    lines: list[str] = []
    for index in range(screen.number_of_lines):
        line = screen.line(index).string.replace("\x00", " ").replace("\xa0", " ")
        lines.append(line.rstrip())
    return lines


def lines_to_text(lines: list[str]) -> str:
    """将屏幕行列表合并为文本，并移除末尾空行。"""
    lines = list(lines)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _is_shell_prompt_line(line: str) -> bool:
    """判断一行是否像 zsh/bash 的空 shell prompt。"""
    stripped = line.replace("\x00", "").replace("\xa0", " ").strip()
    return stripped.endswith("%") or stripped.endswith("$") or stripped.endswith("#")


def parse_tab_number(value: str) -> int:
    """解析用户输入的 tab 编号。"""
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise RuntimeError("tab 编号必须是整数") from exc
    if number <= 0:
        raise RuntimeError("tab 编号必须大于 0")
    return number


def output_after(before_output: str, current_output: str, submitted_text: str) -> str:
    """按本轮输入文本锚点截取新增输出，避免 TUI 重绘带出历史会话。"""
    # 策略 1：严格锚点（要求行首有 prompt 标记）
    anchor = find_last_anchor(current_output, submitted_text)
    if anchor >= 0:
        return current_output[anchor:].lstrip()

    # 策略 2：宽松锚点（无 prompt 前缀的 TUI，如 Cursor）
    # 取 submitted_text 在 current_output 中最后一次出现的位置
    loose = _find_loose_anchor(current_output, submitted_text)
    if loose >= 0:
        return current_output[loose:].lstrip()

    # 策略 3：前缀偏移（output 稳定追加时）
    if before_output and current_output.startswith(before_output):
        return current_output[len(before_output):].lstrip("\n")
    return ""


def _input_anchor_candidates(submitted_text: str) -> list[str]:
    """返回可用于匹配本轮输入回显的候选文本。"""
    candidates: list[str] = []
    primary = submitted_text.strip()
    if primary:
        candidates.append(primary)
    non_empty_lines = [line.strip() for line in submitted_text.splitlines() if line.strip()]
    if non_empty_lines:
        tail = non_empty_lines[-1]
        if tail not in candidates:
            candidates.append(tail)
    return candidates


def looks_like_claude_delta(delta: str) -> bool:
    """判断本轮输出是否像 Claude TUI 内容。"""
    return (
        "⏺" in delta
        or "✻" in delta
        or has_claude_ready_prompt(delta)
    )


def is_claude_turn_complete(
    delta: str,
    screen_text: str = "",
    cursor_line: str = "",
    cursor_x: int = -1,
) -> bool:
    """判断 Claude TUI 单轮交互是否已经回到可输入状态。"""
    return (
        has_claude_answer(delta)
        and not has_claude_active_work_after_answer(delta)
        and not has_claude_active_work_after_answer(screen_text)
        and is_claude_prompt_cursor(cursor_line, cursor_x)
        and has_claude_ready_prompt_tail(screen_text)
    )


def has_claude_ready_prompt(delta: str) -> bool:
    """识别 Claude 重新出现的输入提示符。"""
    for line in reversed(delta.splitlines()):
        stripped = normalize_terminal_line(line)
        if not stripped or is_separator_line(stripped):
            continue
        return stripped in {"❯", ">"}
    return False


def is_claude_prompt_cursor(cursor_line: str, cursor_x: int) -> bool:
    """判断光标是否停在 Claude 的空输入提示符处。"""
    normalized = cursor_line.replace("\x00", "").replace("\xa0", " ")
    stripped = normalized.strip()
    if stripped not in {"❯", ">"}:
        return False
    prompt_index = max(normalized.find("❯"), normalized.find(">"))
    return cursor_x > prompt_index


def has_claude_ready_prompt_tail(text: str) -> bool:
    """判断当前屏幕最后一个有效行是否是 Claude 空输入提示符。"""
    for line in reversed(text.splitlines()):
        stripped = normalize_terminal_line(line)
        if not stripped or is_separator_line(stripped):
            continue
        return stripped in {"❯", ">"}
    return False


def clean_claude_delta(delta: str) -> str:
    """移除 Claude TUI 尾部状态行、分隔线和输入提示符。"""
    lines = [line.replace("\x00", " ").replace("\xa0", " ") for line in delta.splitlines()]
    while lines:
        stripped = normalize_terminal_line(lines[-1])
        if not stripped:
            lines.pop()
            continue
        if stripped in {"❯", ">"}:
            lines.pop()
            continue
        if CLAUDE_STATUS_RE.match(stripped):
            lines.pop()
            continue
        if is_separator_line(stripped):
            lines.pop()
            continue
        break

    lines = [line for line in lines if not is_claude_noise_line(line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("⏺ "):
        indent_len = len(lines[0]) - len(lines[0].lstrip())
        lines[0] = " " * indent_len + lines[0].lstrip()[2:].lstrip()
    return "\n".join(lines).strip()


def is_claude_noise_line(line: str) -> bool:
    """识别 Claude TUI 的进度/状态噪声行。"""
    stripped = normalize_terminal_line(line)
    if not stripped:
        return False
    if CLAUDE_STATUS_RE.match(stripped):
        return True
    if CLAUDE_TIP_RE.match(stripped):
        return True
    if CLAUDE_SPINNER_RE.match(stripped):
        return True
    if "⏺" in stripped:
        return False
    if not stripped.endswith("..."):
        return False
    word = stripped[:-3].replace("-", "").replace("'", "").replace(" ", "")
    return word.isalpha()


def normalize_terminal_line(line: str) -> str:
    """移除 iTerm2/TUI 读取出来的填充字符并裁剪空白。"""
    return line.replace("\x00", "").replace("\xa0", " ").strip()


def has_claude_answer(delta: str) -> bool:
    """判断本轮输出是否已经包含 Claude 回答正文。"""
    return any(normalize_terminal_line(line).startswith("⏺") for line in delta.splitlines())


def has_claude_active_work_after_answer(text: str) -> bool:
    """判断 Claude 回答后是否仍有运行中的工具或活跃状态。"""
    if not text:
        return False
    lines = text.splitlines()
    answer_index = -1
    for index, line in enumerate(lines):
        if normalize_terminal_line(line).startswith("⏺"):
            answer_index = index
    if answer_index < 0:
        return False
    for line in lines[answer_index + 1 :]:
        stripped = normalize_terminal_line(line)
        if not stripped:
            continue
        lower = stripped.lower()
        if CLAUDE_SPINNER_RE.match(stripped):
            return True
        if stripped in {"Running…", "Running..."}:
            return True
        if "ctrl+b to run in background" in lower:
            return True
    return False


def is_separator_line(stripped_line: str) -> bool:
    """判断一行是否只是 Claude TUI 的横向分隔线。"""
    return bool(stripped_line) and set(stripped_line) <= {"─", "-"}


def find_last_anchor(current_output: str, submitted_text: str) -> int:
    """定位本轮输入在当前输出中的最后一次出现位置。"""
    candidates = _input_anchor_candidates(submitted_text)
    lines = current_output.splitlines(keepends=True)
    offset = 0
    best_anchor = -1
    for line in lines:
        line_text = line.rstrip("\r\n")
        for candidate in candidates:
            if not candidate:
                continue
            index = line_text.rfind(candidate)
            if index < 0:
                continue
            if is_input_anchor_line(line_text, index) or _is_pasted_input_anchor_line(line_text, index):
                best_anchor = offset + index + len(candidate)
        offset += len(line)
    if best_anchor >= 0:
        return best_anchor
    return -1


def _find_loose_anchor(current_output: str, submitted_text: str) -> int:
    """宽松锚点：找 submitted_text 独占一行的最后一次出现位置（不要求 prompt 前缀）。

    适用于 Cursor 等 TUI 不显示 prompt 前缀的场景。
    仅匹配整行内容等于 submitted_text 的行，避免从 agent 回复正文中截断。
    """
    candidates = _input_anchor_candidates(submitted_text)
    if not candidates:
        return -1
    lines = current_output.splitlines(keepends=True)
    offset = 0
    best = -1
    for line in lines:
        line_text = line.rstrip("\r\n")
        stripped = normalize_terminal_line(line_text)
        stripped_without_bar = stripped.lstrip("┃").strip()
        for candidate in candidates:
            if stripped == candidate or stripped_without_bar == candidate:
                best = offset + len(line_text)
                continue
            index = line_text.rfind(candidate)
            if index >= 0 and _is_pasted_input_anchor_line(line_text, index):
                best = offset + index + len(candidate)
        offset += len(line)
    return best if best >= 0 else -1


def _is_pasted_input_anchor_line(line: str, candidate_index: int) -> bool:
    """判断命中的文本是否出现在 iTerm2 的粘贴回显行里。"""
    if candidate_index <= 0:
        return False
    prefix = normalize_terminal_line(line[:candidate_index])
    return bool(prefix) and PASTED_INPUT_PREFIX_RE.match(prefix) is not None


def is_input_anchor_line(line: str, candidate_index: int) -> bool:
    """判断命中的文本是否位于交互式输入回显行。

    要求行首有明确的 prompt 标记（❯ > $ % # >>>）才算锚点，
    避免回答内容里碰巧出现用户输入文本的行被误判。
    """
    prefix = normalize_terminal_line(line[:candidate_index])
    if not prefix:
        return False
    return (
        prefix in {"❯", ">", ">>>", "...", "┃"}
        or prefix.endswith(("❯", ">", "$", "%", "#", "┃"))
    )


def command_name(command: str) -> str | None:
    """解析 shell 命令中的可执行文件名。"""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        parts = command.strip().split()
    while parts and is_shell_assignment(parts[0]):
        parts.pop(0)
    if not parts:
        return None
    return os.path.basename(parts[0])


def is_shell_assignment(value: str) -> bool:
    """判断片段是否是 shell 环境变量赋值。"""
    name, sep, _raw = value.partition("=")
    return bool(sep) and name.replace("_", "").isalnum() and not name[0].isdigit()


def _has_interactive_prompt_tail(text: str) -> bool:
    """判断屏幕最后一个有效行是否是交互式 CLI 的空输入提示符（> 或 ❯）。"""
    for line in reversed(text.splitlines()):
        stripped = normalize_terminal_line(line)
        if not stripped or _is_separator_generic(stripped):
            continue
        return stripped in {">", "❯"}
    return False


def _has_cursor_ready_state(text: str) -> bool:
    """判断 Cursor 是否回到了可继续追问的完成态。"""
    if _has_interactive_prompt_tail(text):
        return True
    for line in text.splitlines():
        stripped = normalize_terminal_line(line)
        if CURSOR_FOLLOW_UP_RE.match(stripped):
            return True
    return False


def _has_substantive_content(delta: str) -> bool:
    """判断 delta 中是否包含非提示符、非噪声的实质内容。"""
    for line in delta.splitlines():
        stripped = normalize_terminal_line(line)
        if not stripped:
            continue
        if _is_generic_noise_line(stripped):
            continue
        return True
    return False


def _has_cursor_answer(delta: str) -> bool:
    """判断 Cursor 输出里是否已出现真正回答正文。"""
    return bool(_clean_generic_delta(delta).strip())


def _has_opencode_answer(delta: str) -> bool:
    """判断 OpenCode 输出里是否已出现真正回答正文。"""
    return bool(_clean_opencode_delta(delta).strip())


def _is_separator_generic(stripped: str) -> bool:
    """判断一行是否为分隔线（通用）。"""
    return bool(stripped) and set(stripped) <= {"─", "-", "━", "═", "▄", "▀"}


def _is_generic_noise_line(stripped: str) -> bool:
    """判断一行是否属于交互 CLI 的噪声/UI 提示。"""
    if not stripped:
        return False
    if stripped in {">", "❯", "~", "<system-reminder>", "</system-reminder>", "Auto-run"}:
        return True
    if _is_separator_generic(stripped):
        return True
    if PASTED_INPUT_PREFIX_RE.match(stripped):
        return True
    if CURSOR_FOLLOW_UP_RE.match(stripped):
        return True
    if CURSOR_COMPOSER_STATUS_RE.match(stripped):
        return True
    if stripped in {"Greeting", "Context", "LSP", "LSPs are disabled"}:
        return True
    if OPENCODE_TOKENS_RE.match(stripped):
        return True
    if OPENCODE_PERCENT_RE.match(stripped):
        return True
    if OPENCODE_COST_RE.match(stripped):
        return True
    if OPENCODE_COMMANDS_RE.match(stripped):
        return True
    if stripped.startswith("▣  Build"):
        return True
    if stripped.startswith("Build · "):
        return True
    if stripped.startswith("Your operational mode has changed from "):
        return True
    if stripped.startswith("You are no longer in "):
        return True
    if stripped.startswith("You are permitted to "):
        return True
    return False


def _is_opencode_noise_line(stripped: str) -> bool:
    """判断一行是否属于 OpenCode TUI 的输入栏、侧栏或状态栏。"""
    if _is_generic_noise_line(stripped):
        return True
    if stripped.startswith("┃"):
        return True
    if "esc interrupt" in stripped.lower():
        return True
    if stripped.startswith("╹") and set(stripped[1:]) <= {"─", "-", "━", "═", "▄", "▀", " ", "▌", "▐", "█"}:
        return True
    return False


def _clean_opencode_delta(delta: str) -> str:
    """清理 OpenCode 的输入栏、思考侧栏和底部状态栏。"""
    lines = [
        line.replace("\x00", " ").replace("\xa0", " ")
        for line in delta.splitlines()
    ]
    while lines:
        stripped = normalize_terminal_line(lines[-1])
        if not stripped:
            lines.pop()
            continue
        if _is_opencode_noise_line(stripped):
            lines.pop()
            continue
        break
    lines = [line for line in lines if not _is_opencode_noise_line(normalize_terminal_line(line))]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _clean_generic_delta(delta: str) -> str:
    """通用的 TUI 输出清理：移除尾部提示符和分隔线。"""
    lines = [
        line.replace("\x00", " ").replace("\xa0", " ")
        for line in delta.splitlines()
    ]
    while lines:
        stripped = normalize_terminal_line(lines[-1])
        if not stripped:
            lines.pop()
            continue
        if _is_generic_noise_line(stripped):
            lines.pop()
            continue
        break
    lines = [line for line in lines if not _is_generic_noise_line(normalize_terminal_line(line))]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()
