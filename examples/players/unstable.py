#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例玩家 E：故意违规（用来验证中转站的异常处理）

config.mode 可选：
    crash    跑到一半直接退出（模拟崩溃）
    garbage  输出非 JSON（模拟协议错误）
    slow     睡死（模拟超时）
    empty    返回 null（模拟空动作）
    mix      轮流触发以上四种（默认）

在席位参数里填 {"mode": "slow"} 就能指定；不填则按 mix 轮换。
"""

import json
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def main():
    init = recv()
    cfg = init.get("config") or {}
    mode = str(cfg.get("mode", "mix"))
    send({"v": 1, "type": "ready"})

    n = 0
    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            n += 1
            cur = mode if mode != "mix" else ["garbage", "slow", "empty", "crash"][(n - 1) % 4]
            if cur == "crash":
                sys.stderr.write("模拟崩溃：主动退出\n")
                sys.exit(3)
            if cur == "garbage":
                sys.stdout.write("这不是 JSON，这是我在摆烂\n")
                sys.stdout.flush()
                continue
            if cur == "slow":
                time.sleep(30)
                continue
            if cur == "empty":
                send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": None})
                continue
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": {"bid": 1}})
        elif mtype == "over":
            break
        elif mtype == "shutdown":
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
