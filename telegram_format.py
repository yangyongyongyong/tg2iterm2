"""Markdown 转 Telegram 富文本格式工具。

Telegram Bot API 支持的 HTML 标签有限：
<b>, <i>, <u>, <s>, <code>, <pre>, <a href="">, <tg-spoiler>

本模块将 CLI 输出中的 Markdown 格式转为 Telegram 兼容 HTML。
解析失败时回退为 <pre> 包裹的纯文本。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class TelegramEntityChunk:
    """保存可直接发送给 Telegram API 的文本片段。"""

    text: str
    entities: list[dict[str, Any]]


def md_to_telegram_entities(text: str, max_utf16_length: int = 4000) -> list[TelegramEntityChunk]:
    """将 Markdown 转成 Telegram entities 片段。

    优先复用 telegramify-markdown；如果运行环境未安装该库或转换失败，
    返回空列表，调用方可继续走 HTML/纯文本回退链路。
    """
    try:
        from telegramify_markdown import convert as tg_convert
        from telegramify_markdown import split_entities
    except Exception:
        return []

    try:
        plain_text, entities = tg_convert(text)
    except Exception:
        return []

    if not plain_text:
        return []

    try:
        chunks = split_entities(plain_text, entities, max_utf16_len=max_utf16_length)
    except Exception:
        return [TelegramEntityChunk(text=plain_text, entities=_normalize_entities(entities))]

    result: list[TelegramEntityChunk] = []
    for chunk_text, chunk_entities in chunks:
        if not chunk_text:
            continue
        result.append(
            TelegramEntityChunk(
                text=chunk_text,
                entities=_normalize_entities(chunk_entities),
            )
        )
    return result


def _normalize_entities(raw_entities: Any) -> list[dict[str, Any]]:
    """把第三方库返回的 entity 对象统一转为 dict 列表。"""
    normalized: list[dict[str, Any]] = []
    if raw_entities is None:
        return normalized
    for entity in raw_entities:
        if isinstance(entity, dict):
            normalized.append(entity)
            continue
        to_dict = getattr(entity, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
            if isinstance(value, dict):
                normalized.append(value)
    return normalized


def md_to_telegram_html(text: str) -> str:
    """将 Markdown 文本转换为 Telegram 兼容 HTML。

    支持的 Markdown 语法：
    - # 标题 → <b>标题</b>
    - **粗体** → <b>粗体</b>
    - *斜体* / _斜体_ → <i>斜体</i>
    - ~~删除线~~ → <s>删除线</s>
    - `行内代码` → <code>行内代码</code>
    - ```代码块``` → <pre>代码块</pre>
    - [链接](url) → <a href="url">链接</a>
    - Markdown 表格 → <pre> 等宽渲染
    """
    try:
        return _convert(text)
    except Exception:
        return f"<pre>{_escape(text)}</pre>"


def _escape(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _convert(text: str) -> str:
    """执行 Markdown → Telegram HTML 转换。"""
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 代码块 ```...```
        if line.strip().startswith("```"):
            code_lines: list[str] = []
            lang = line.strip()[3:].strip()
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code = "\n".join(code_lines)
            if lang:
                result.append(f'<pre><code class="language-{_escape(lang)}">{_escape(code)}</code></pre>')
            else:
                result.append(f"<pre>{_escape(code)}</pre>")
            i += 1
            continue

        # Markdown 表格（包含 | 的连续行）
        if "|" in line and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            table_lines = [line]
            j = i + 1
            while j < len(lines) and "|" in lines[j]:
                table_lines.append(lines[j])
                j += 1
            result.append(f"<pre>{_escape(chr(10).join(table_lines))}</pre>")
            i = j
            continue

        # 标题
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            content = _inline_format(heading_match.group(2))
            result.append(f"<b>{content}</b>")
            i += 1
            continue

        # 水平线
        if re.match(r"^[-*_]{3,}\s*$", line.strip()):
            result.append("—" * 20)
            i += 1
            continue

        # 普通行：行内格式化
        result.append(_inline_format(line))
        i += 1

    return "\n".join(result)


def _inline_format(text: str) -> str:
    """处理行内 Markdown 格式。"""
    text = _escape(text)

    # 行内代码 `code`（先处理，避免内部被其他规则干扰）
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # 粗体 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 粗体 __text__
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 斜体 *text*（不匹配列表项 * ）
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)

    # 斜体 _text_（不匹配 __）
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text)

    # 删除线 ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 链接 [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    return text


def _is_table_separator(line: str) -> bool:
    """判断是否为 Markdown 表格分隔行（|---|---|）。"""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [c.strip() for c in stripped.split("|") if c.strip()]
    return all(re.match(r"^:?-+:?$", cell) for cell in cells)
