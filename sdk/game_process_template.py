#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""进程式游戏模板 —— 复制这个文件，改成你自己的游戏。

跑起来后中转站会自动拉起它。要点：
1. stdout 只能输出 JSON 行（一行一个对象），调试信息走 stderr；
2. 自己维护局面与回合循环，中转站不解释任何规则；
3. 每个玩家的 view 单独拼，别把别人的私密信息写进去。
"""

import json
import random
import sys

V = 1


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if line:
            return json.loads(line)


def main():
    # ---------- 1. 初始化 ----------
    init = recv()                       # type == "init"
    players = [p["id"] for p in init["players"]]
    names = {p["id"]: p.get("name", p["id"]) for p in init["players"]}
    cfg = init.get("config") or {}
    rounds = int(cfg.get("rounds", 5))
    rng = random.Random(int(init.get("seed", 0)))

    state = {p: 0 for p in players}      # 你的局面：这里用分数占位

    send({"v": V, "type": "ready", "name": "我的进程式游戏",
          "players": {"min": 2, "max": 8}})

    if recv().get("type") != "run":
        send({"v": V, "type": "error", "message": "期望 run"})
        return

    # ---------- 2. 回合循环 ----------
    for turn in range(1, rounds + 1):
        public = {"turn": turn, "rounds": rounds,
                  "players": [{"id": p, "name": names[p]} for p in players],
                  "scores": dict(state)}
        requests = [
            {
                "player": p,
                "view": {
                    "public": public,
                    # 只有这个人能看到的东西放 private
                    "private": {"secret": rng.randint(1, 9), "score": state[p]},
                },
            }
            for p in players
        ]
        send({"v": V, "type": "ask", "turn": turn, "requests": requests})

        msg = recv()                     # type == "actions"
        if msg.get("type") == "shutdown":
            return
        actions = msg.get("actions") or {}

        # ---------- 3. 结算（记得处理 ok=False） ----------
        for p in players:
            res = actions.get(p) or {}
            if not res.get("ok"):
                continue                 # 超时 / 崩溃 / 判负 → 视为弃权
            act = res.get("action") or {}
            try:
                gain = int(act.get("value", 0))
            except (TypeError, ValueError):
                gain = 0
            state[p] += max(0, min(gain, 10))

        send({"v": V, "type": "log",
              "text": f"第 {turn} 回合结束：" + "、".join(
                  f"{names[p]} {state[p]}" for p in players)})

    # ---------- 4. 结束 ----------
    ranking = sorted(players, key=lambda p: -state[p])
    send({"v": V, "type": "over", "result": {
        "ranking": ranking,
        "scores": {p: float(state[p]) for p in players},
        "winner": ranking[0],
        "reason": f"{rounds} 回合结束",
    }})


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
