#!/usr/bin/env python3
"""Claude Code PermissionRequest hook — 桥接到 tg2iterm2 bot。

由 Claude Code 在 PermissionRequest 事件时作为子进程调用。
从 stdin 读取 JSON 请求，写入文件等待 bot 转发给 Telegram 用户，
用户通过 Inline Keyboard 决定后 bot 写入响应文件，本脚本读取后返回。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REQUEST_FILE = Path("/tmp/tg2iterm2_perm_request.json")
RESPONSE_FILE = Path("/tmp/tg2iterm2_perm_response.json")
TIMEOUT = 120


def main() -> None:
    request_data = json.loads(sys.stdin.read())

    RESPONSE_FILE.unlink(missing_ok=True)
    REQUEST_FILE.write_text(json.dumps(request_data, ensure_ascii=False))

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if RESPONSE_FILE.exists():
            try:
                response = json.loads(RESPONSE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                time.sleep(0.3)
                continue
            RESPONSE_FILE.unlink(missing_ok=True)
            REQUEST_FILE.unlink(missing_ok=True)
            print(json.dumps(response))
            return
        time.sleep(0.3)

    REQUEST_FILE.unlink(missing_ok=True)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Telegram 用户未响应，自动拒绝",
        }
    }))


if __name__ == "__main__":
    main()
