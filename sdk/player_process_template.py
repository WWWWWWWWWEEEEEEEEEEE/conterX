#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""进程式玩家模板 —— 你的算法就从这里开始。

协议只有三步：init → ready，turn → action，over → 退出。
常驻模式下进程会跨回合活着，可以放心在内存里维护搜索树、统计表。
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


# 跨回合状态放这里（常驻模式下有效）
memory = {"rounds_seen": 0, "my_bids": []}


def decide(view, turn, cfg):
    public = view.get("public") or {}
    private = view.get("private") or {}
    memory["rounds_seen"] += 1

    # ↓↓↓ 换成你的算法 ↓↓↓
    # 模板游戏要的是 {"value": 0~10}，落后就多拿一点
    my_score = int(private.get("score", 0))
    value = 4 if my_score < 10 else 1
    # ↑↑↑ 换成你的算法 ↑↑↑

    memory["my_bids"].append(value)
    return {"value": value}


def main():
    init = recv()
    cfg = init.get("config") or {}
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            turn = msg.get("turn")
            action = decide(msg.get("view") or {}, turn, cfg)
            send({"v": 1, "type": "action", "turn": turn, "action": action})
        elif mtype == "log":
            continue
        elif mtype in ("over", "shutdown"):
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
