"""动态扫描 Claude Code 可用 skills，供 tg bot 启动时刷新菜单。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PLUGINS_DIR = Path.home() / ".claude" / "plugins"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
YAML_KV_RE = re.compile(r"^(\w[\w-]*):\s*(.+)$", re.MULTILINE)

# Claude Code 内置 skills（不在插件目录中，需要硬编码）
BUILTIN_SKILLS: list[tuple[str, str]] = [
    ("init", "初始化 CLAUDE.md 文件"),
    ("review", "审查 Pull Request"),
    ("security-review", "安全审查"),
    ("simplify", "审查代码质量和效率"),
    ("fewer-permission-prompts", "减少权限弹窗"),
    ("loop", "循环执行命令"),
    ("update-config", "配置 settings.json"),
    ("keybindings-help", "自定义快捷键"),
    ("schedule", "定时任务调度"),
    ("claude-api", "Claude API 开发调试"),
    ("agent-browser", "浏览器自动化"),
]


@dataclass
class SkillInfo:
    name: str
    description: str
    source: str  # "builtin" or plugin name


def scan_all_skills() -> list[SkillInfo]:
    """扫描所有可用 skills：内置 + 已启用插件。"""
    skills: list[SkillInfo] = []

    # 内置 skills
    for name, desc in BUILTIN_SKILLS:
        skills.append(SkillInfo(name=name, description=desc, source="builtin"))

    # 插件 skills
    enabled_plugins = _get_enabled_plugins()
    for plugin_key in enabled_plugins:
        plugin_skills = _scan_plugin_skills(plugin_key)
        skills.extend(plugin_skills)

    return skills


def skill_to_tg_command(skill_name: str) -> str:
    """将 skill 名转为 Telegram 合法命令名（小写字母+数字+下划线，最长32字符）。"""
    # 对 plugin:skill 格式，只取冒号后的 skill 部分作为主名
    if ":" in skill_name:
        parts = skill_name.split(":")
        # 取插件名缩写 + skill名
        plugin_abbr = parts[0].split("-")[-1]  # 取最后一段
        cmd = f"{plugin_abbr}_{parts[1]}"
    else:
        cmd = skill_name
    cmd = cmd.replace("-", "_").replace(":", "_").replace(".", "_")
    cmd = re.sub(r"[^a-z0-9_]", "", cmd.lower())
    cmd = f"sk_{cmd}"
    if len(cmd) > 32:
        cmd = cmd[:32]
    # 去掉末尾下划线
    cmd = cmd.rstrip("_")
    return cmd


def build_skill_map(skills: list[SkillInfo]) -> dict[str, str]:
    """构建 TG命令 → 原始skill名 的映射。"""
    return {skill_to_tg_command(s.name): s.name for s in skills}


def build_tg_commands(skills: list[SkillInfo]) -> list[dict[str, str]]:
    """构建 Telegram setMyCommands 需要的命令列表。"""
    commands = []
    for skill in skills:
        cmd = skill_to_tg_command(skill.name)
        desc = f"[skill] {skill.description}"
        if len(desc) > 256:
            desc = desc[:253] + "..."
        commands.append({"command": cmd, "description": desc})
    return commands


def _get_enabled_plugins() -> list[str]:
    """从 settings.json 读取已启用的插件列表。"""
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
        enabled = settings.get("enabledPlugins", {})
        return [key for key, val in enabled.items() if val]
    except (OSError, json.JSONDecodeError):
        return []


def _scan_plugin_skills(plugin_key: str) -> list[SkillInfo]:
    """扫描单个插件的 skills 和 commands。

    plugin_key 格式: "plugin-name@marketplace-name"
    """
    parts = plugin_key.split("@", 1)
    if len(parts) != 2:
        return []
    plugin_name, marketplace_name = parts
    marketplace_dir = PLUGINS_DIR / "marketplaces" / marketplace_name

    # 找到插件的 plugin.json
    plugin_json_path = marketplace_dir / ".claude-plugin" / "plugin.json"
    plugin_dir = marketplace_dir

    if not plugin_json_path.exists():
        # 可能是 marketplace 内的子插件
        sub_plugin_dir = marketplace_dir / "plugins" / plugin_name
        if sub_plugin_dir.exists():
            plugin_dir = sub_plugin_dir
            plugin_json_path = sub_plugin_dir / ".claude-plugin" / "plugin.json"
            if not plugin_json_path.exists():
                plugin_json_path = sub_plugin_dir / "plugin.json"

    skills: list[SkillInfo] = []

    # 读取 plugin.json
    plugin_data = _read_json(plugin_json_path)
    actual_plugin_name = plugin_data.get("name", plugin_name) if plugin_data else plugin_name

    # 扫描 skills 目录
    skills_entries = plugin_data.get("skills", []) if plugin_data else []
    for entry in skills_entries:
        skill_dir = plugin_dir / entry
        skill_md = skill_dir / "SKILL.md" if skill_dir.is_dir() else skill_dir.with_suffix(".md")
        if not skill_md.exists():
            skill_md = skill_dir / "SKILL.md"
        info = _parse_skill_md(skill_md, actual_plugin_name)
        if info:
            skills.append(info)

    # 扫描 commands 目录
    commands_entries = plugin_data.get("commands", []) if plugin_data else []
    for entry in commands_entries:
        cmd_path = plugin_dir / entry
        if not cmd_path.exists():
            continue
        info = _parse_command_md(cmd_path, actual_plugin_name)
        if info:
            skills.append(info)

    # 如果 plugin.json 不存在，尝试直接扫描 skills/ 和 commands/ 目录
    if not plugin_data:
        for skill_md in plugin_dir.glob("skills/*/SKILL.md"):
            info = _parse_skill_md(skill_md, plugin_name)
            if info:
                skills.append(info)
        for cmd_md in plugin_dir.glob("commands/*.md"):
            info = _parse_command_md(cmd_md, plugin_name)
            if info:
                skills.append(info)

    return skills


def _parse_skill_md(path: Path, plugin_name: str) -> SkillInfo | None:
    """解析 SKILL.md 的 frontmatter 获取 name 和 description。"""
    if not path.exists():
        return None
    content = path.read_text(errors="ignore")
    fm = _parse_frontmatter(content)
    name = fm.get("name", "")
    if not name:
        name = path.parent.name
    desc = fm.get("description", name)
    # 带上插件前缀形成完整 skill 名
    full_name = f"{plugin_name}:{name}" if plugin_name != name else name
    return SkillInfo(name=full_name, description=desc[:100], source=plugin_name)


def _parse_command_md(path: Path, plugin_name: str) -> SkillInfo | None:
    """解析 command md 的 frontmatter。"""
    if not path.exists():
        return None
    content = path.read_text(errors="ignore")
    fm = _parse_frontmatter(content)
    name = path.stem
    desc = fm.get("description", name)
    full_name = f"{plugin_name}:{name}"
    return SkillInfo(name=full_name, description=desc[:100], source=plugin_name)


def _parse_frontmatter(content: str) -> dict[str, str]:
    """简易 YAML frontmatter 解析（只取 key: value 形式）。"""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}
    block = match.group(1)
    return {m.group(1): m.group(2).strip() for m in YAML_KV_RE.finditer(block)}


def _read_json(path: Path) -> dict | None:
    """安全读取 JSON 文件。"""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
