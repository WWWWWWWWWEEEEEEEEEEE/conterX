#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""暗标竞拍（进程式游戏示例）

规则
----
- 每位玩家开局 100 金币，每回合抽一件拍品，每人拿到一个**私有估值**（20~60）。
- 所有人同时暗标出价（整数，0 ≤ 出价 ≤ 剩余金币）。
- 最高价成交：赢家付出出价、获得"估值"分；若最高价并列则流拍。
- 共 8 回合，最终按累计得分排名，同分比剩余金币。

这个文件同时是**进程式游戏**的活文档：只依赖 stdin/stdout 的 JSON 行，
不 import 中转站的任何代码，任何语言都能照着写。
"""

import json
import sys

V = 1
ITEMS = [
    "青玉算盘", "黄铜罗盘", "断齿齿轮", "雨声留声机", "无名者手札",
    "锈铁怀表", "玻璃蝴蝶", "半张星图", "旧式电报键", "第七枚棋子",
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue


def log(text, level="info"):
    send({"v": V, "type": "log", "text": text, "level": level})


def main():
    import random

    init = recv()
    if init.get("type") != "init":
        send({"v": V, "type": "error", "message": "期望 init"})
        return

    players = [p["id"] for p in init.get("players", [])]
    names = {p["id"]: p.get("name", p["id"]) for p in init.get("players", [])}
    if len(players) < 2:
        send({"v": V, "type": "error", "message": "至少需要 2 名玩家"})
        return

    cfg = init.get("config") or {}
    rounds = int(cfg.get("rounds", 8))
    start_budget = int(cfg.get("budget", 100))
    rng = random.Random(int(init.get("seed", 0)))

    budget = {p: start_budget for p in players}
    score = {p: 0.0 for p in players}
    won = {p: [] for p in players}
    history = []

    send({"v": V, "type": "ready", "name": "暗标竞拍",
          "players": {"min": 2, "max": 8},
          "meta": {"rounds": rounds}})

    msg = recv()
    if msg.get("type") != "run":
        send({"v": V, "type": "error", "message": "期望 run"})
        return

    log(f"竞拍开始：{len(players)} 人，{rounds} 回合，每人 {start_budget} 金币")

    for r in range(1, rounds + 1):
        item = rng.choice(ITEMS)
        valuation = {p: rng.randint(20, 60) for p in players}

        public = {
            "round": r,
            "rounds": rounds,
            "item": item,
            "players": [{"id": p, "name": names[p]} for p in players],
            "scores": {p: score[p] for p in players},
            "history": history[-3:],
        }
        requests = []
        for p in players:
            requests.append({
                "player": p,
                "view": {
                    "public": public,
                    "private": {
                        "budget": budget[p],
                        "valuation": valuation[p],
                        "score": score[p],
                        "won": won[p],
                    },
                },
            })
        send({"v": V, "type": "ask", "turn": r, "requests": requests})

        msg = recv()
        if msg.get("type") == "shutdown":
            return
        actions = msg.get("actions") or {}

        bids = {}
        illegal = []
        for p in players:
            res = actions.get(p) or {}
            if not res.get("ok"):
                bids[p] = None          # 超时 / 崩溃 / 判负 → 本回合弃权
                continue
            act = res.get("action")
            raw = act.get("bid") if isinstance(act, dict) else None
            try:
                bid = int(raw)
            except (TypeError, ValueError):
                illegal.append(p)
                bid = 0
            if bid < 0 or bid > budget[p]:
                illegal.append(p)
                bid = max(0, min(bid, budget[p]))
            bids[p] = bid

        valid = {p: b for p, b in bids.items() if b is not None}
        winner = None
        price = 0
        if valid:
            price = max(valid.values())
            topers = [p for p, b in valid.items() if b == price]
            if price > 0 and len(topers) == 1:
                winner = topers[0]

        if winner:
            budget[winner] -= price
            gain = valuation[winner] - price
            score[winner] += gain
            won[winner].append(item)
            log(f"第 {r} 回合 · {item}：{names[winner]} 以 {price} 拍得"
                f"（估值 {valuation[winner]}，净收益 {gain:+d}）")
        else:
            log(f"第 {r} 回合 · {item}：流拍（最高价 {price} 并列或无人出价）")

        if illegal:
            log("非法出价已按规则修正：" + "、".join(names[p] for p in sorted(set(illegal))),
                level="warn")

        history.append({
            "round": r, "item": item, "winner": winner,
            "winner_name": names.get(winner, "") if winner else "",
            "price": price, "sold": bool(winner),
        })

    ranking = sorted(players, key=lambda p: (-score[p], budget[p]))
    reason = f"{rounds} 回合结束"
    send({"v": V, "type": "over", "result": {
        "ranking": ranking,
        "scores": {p: score[p] for p in players},
        "winner": ranking[0] if ranking else None,
        "reason": reason,
        "extra": {"budget": budget},
    }})


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
