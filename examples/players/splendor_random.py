#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · 随机玩家（基线对手）

从 legal_actions 里随便挑一个。用来验证协议、跑批量对局、当陪练下限。
"""

import json
import random
import sys

rng = random.Random()


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
    if cfg.get("seed") is not None:
        rng.seed(int(cfg["seed"]))
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            legal = (view.get("private") or {}).get("legal_actions") or []
            action = rng.choice(legal) if legal else {"action": "pass"}
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": action})
        elif mtype in ("over", "shutdown"):
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
