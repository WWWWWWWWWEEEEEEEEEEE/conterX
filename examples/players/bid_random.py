#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例玩家 A：随机出价（进程式玩家模板）

所有进程式玩家都长这样：
    init  -> ready
    turn  -> action
    over  -> 退出
视野里 public 是所有人可见的信息，private 只有你自己能拿到。
"""

import json
import random
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def main():
    init = recv()
    cfg = init.get("config") or {}
    rng = random.Random(int(cfg.get("seed", 0)) or None)
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            private = view.get("private") or {}
            budget = int(private.get("budget", 0))
            # 随机在预算内出价，纯陪跑
            bid = rng.randint(0, max(0, budget))
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": {"bid": bid}})
        elif mtype == "over":
            break
        elif mtype == "shutdown":
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
