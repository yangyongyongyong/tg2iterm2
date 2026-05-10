#!/bin/bash
# 一键安装 Claude + Cursor hooks 到 tg2iterm2 bot
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 安装 tg2iterm2 hooks ==="
echo ""

# ─── Claude hooks ───
CLAUDE_HOOK="$SCRIPT_DIR/claude_hook.py"
CLAUDE_SETTINGS_DIR="$HOME/.claude"
CLAUDE_SETTINGS="$CLAUDE_SETTINGS_DIR/settings.json"

if [ -f "$CLAUDE_HOOK" ]; then
    chmod +x "$CLAUDE_HOOK"
    mkdir -p "$CLAUDE_SETTINGS_DIR"
    [ -f "$CLAUDE_SETTINGS" ] || echo '{}' > "$CLAUDE_SETTINGS"

    python3 -c "
import json
from pathlib import Path

settings_path = Path('$CLAUDE_SETTINGS')
hook_command = '$CLAUDE_HOOK'

settings = json.loads(settings_path.read_text())
hooks = settings.setdefault('hooks', {})
perm_hooks = hooks.setdefault('PermissionRequest', [])

already = any(
    any(h.get('command', '').endswith('claude_hook.py') for h in entry.get('hooks', []))
    for entry in perm_hooks
)
if not already:
    perm_hooks.append({
        'matcher': '',
        'hooks': [{'type': 'command', 'command': hook_command}]
    })
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
    print(f'Claude hook 已注册: {hook_command}')
else:
    print('Claude hook 已存在，跳过')
"
else
    echo "跳过 Claude hook（未找到 $CLAUDE_HOOK）"
fi

echo ""

# ─── Cursor hooks ───
CURSOR_HOOK="$SCRIPT_DIR/cursor_hook.py"
CURSOR_HOOKS_DIR="$HOME/.cursor"
CURSOR_HOOKS_JSON="$CURSOR_HOOKS_DIR/hooks.json"

if [ -f "$CURSOR_HOOK" ]; then
    chmod +x "$CURSOR_HOOK"
    mkdir -p "$CURSOR_HOOKS_DIR"

    python3 -c "
import json
from pathlib import Path

hooks_path = Path('$CURSOR_HOOKS_JSON')
hook_command = '$CURSOR_HOOK'

if hooks_path.exists():
    config = json.loads(hooks_path.read_text())
else:
    config = {'version': 1, 'hooks': {}}

hooks = config.setdefault('hooks', {})

# preToolUse hook
pre_hooks = hooks.setdefault('preToolUse', [])
has_pre = any(h.get('command', '').endswith('cursor_hook.py') for h in pre_hooks)
if not has_pre:
    pre_hooks.append({'command': hook_command, 'matcher': 'Shell|Write|Edit|Delete'})

# stop hook
stop_hooks = hooks.setdefault('stop', [])
has_stop = any(h.get('command', '').endswith('cursor_hook.py') for h in stop_hooks)
if not has_stop:
    stop_hooks.append({'command': hook_command})

hooks_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
print(f'Cursor hooks 已注册: {hook_command}')
"
else
    echo "跳过 Cursor hook（未找到 $CURSOR_HOOK）"
fi

echo ""
echo "完成！重启 Claude Code / Cursor 后生效。"
echo "确保 tg2iterm2 bot 正在运行，权限请求会通过 Telegram 按钮确认。"
