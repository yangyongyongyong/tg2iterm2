#!/bin/bash
# 将 permission_hook.py 注册到 Claude Code 项目级 settings
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PATH="$SCRIPT_DIR/permission_hook.py"

if [ ! -f "$HOOK_PATH" ]; then
    echo "错误: 找不到 $HOOK_PATH"
    exit 1
fi

chmod +x "$HOOK_PATH"

SETTINGS_DIR="$HOME/.claude"
SETTINGS_FILE="$SETTINGS_DIR/settings.json"

mkdir -p "$SETTINGS_DIR"

if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
fi

# 使用 python 来安全地合并 JSON
python3 -c "
import json
from pathlib import Path

settings_path = Path('$SETTINGS_FILE')
hook_command = '$HOOK_PATH'

settings = json.loads(settings_path.read_text())

hooks = settings.setdefault('hooks', {})
perm_hooks = hooks.setdefault('PermissionRequest', [])

# 检查是否已注册
already_registered = any(
    any(h.get('command', '').endswith('permission_hook.py') for h in entry.get('hooks', []))
    for entry in perm_hooks
)

if not already_registered:
    perm_hooks.append({
        'matcher': '',
        'hooks': [
            {
                'type': 'command',
                'command': hook_command
            }
        ]
    })
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
    print(f'已注册 hook: {hook_command}')
    print(f'配置文件: {settings_path}')
else:
    print('Hook 已注册，无需重复添加')
"

echo ""
echo "完成！重启 Claude Code 后生效。"
echo "确保 tg2iterm2 bot 正在运行，权限请求会通过 Telegram 按钮确认。"
