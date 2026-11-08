#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例玩家 B：按估值顶价出价（激进的"老实人"）

只要估值高于出价就赚，所以直接出到 估值-1，把利润压到最薄也要抢到拍品。
预算不够就全押。这种打法会推高成交价，逼其他人退出。
"""

import json
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
    margin = int(cfg.get("margin", 1))
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            private = view.get("private") or {}
            budget = int(private.get("budget", 0))
            valuation = int(private.get("valuation", 0))
            bid = max(0, min(budget, valuation - margin))
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
