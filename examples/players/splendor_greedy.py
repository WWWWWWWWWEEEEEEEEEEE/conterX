#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · 启发式玩家

思路（一个能打的基线，不是最优解）：
1. 能买就买：按「分数 + 贵族进度 + 等级经济性 - 费用」打分，挑最高分的那张；
2. 买不起就看离目标卡差多少，差得不多就攒宝石（优先攒最缺的颜色）；
3. 手里没牌、又盯着大牌，就预约一张高分牌顺便拿黄金；
4. 宝石快满 10 枚时不再乱拿，避免被强制弃置。

所有判断都基于 view 里的公开信息 + 自己的 private，不看别人的暗牌。
"""

import json
import sys

COLORS = ["white", "black", "red", "green", "blue"]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


# ---------------------------------------------------------------------------
def card_score(card, me, nobles, target_cost=None):
    """给一张卡打分：分数权重最高，其次看是否推进贵族、是否补齐目标卡的费用。"""
    if not card:
        return -999
    s = card["points"] * 12.0
    bonus = card["bonus"]
    have = me["bonuses"].get(bonus, 0)
    for n in nobles:
        req = n["requires"]
        if bonus in req:
            s += 3.0 if have + 1 >= req[bonus] else 1.5
    if target_cost and target_cost.get(bonus):
        s += 4.0 + target_cost[bonus]      # 这张卡的加成正好抵扣目标卡
    # 用「扣掉永久加成后的实际花费」衡量代价：已经铺好的颜色几乎白拿
    eff = 0
    for c, v in card["cost"].items():
        eff += max(0, v - me["bonuses"].get(c, 0))
    s += (4 - card["tier"]) * 2.0          # 低级牌前期更实用
    s -= eff * 1.4
    return s


def shortfall(card, me):
    """还需要凑多少宝石（已扣掉永久加成和手牌）。"""
    need = {}
    for c, v in card["cost"].items():
        n = v - me["bonuses"].get(c, 0) - me["tokens"].get(c, 0)
        if n > 0:
            need[c] = n
    return need


def decide(view):
    pub = view.get("public") or {}
    priv = view.get("private") or {}
    legal = priv.get("legal_actions") or []
    if not legal:
        return {"action": "pass"}

    me = None
    for p in pub.get("players") or []:
        if p["id"] == priv.get("player_id"):
            me = p
            break
    if me is None:
        return legal[0]

    nobles = pub.get("nobles") or []
    board = []
    for t in ("1", "2", "3"):
        for c in (pub.get("board") or {}).get(t) or []:
            if c:
                board.append(c)
    reserved = priv.get("reserved") or []
    by_id = {c["id"]: c for c in board + reserved}
    my_tokens = sum((me.get("tokens") or {}).values()) + me.get("gold", 0)
    gold = me.get("gold", 0)
    bonus_total = sum((me.get("bonuses") or {}).values())

    # 目标：现有牌里「价值 - 还差多少」最高的一张
    targets = []
    for c in board + reserved:
        need = shortfall(c, me)
        gap = max(0, sum(need.values()) - gold)
        targets.append((card_score(c, me, nobles) - gap * 2.2, c, need, gap))
    targets.sort(key=lambda x: -x[0])
    target = targets[0] if targets else None

    # 1. 能买就买，但别为了买而买
    tgt_cost = target[1]["cost"] if target else None
    buys = [a for a in legal if a["action"] == "buy"]
    if buys:
        best = max(buys, key=lambda a: card_score(by_id.get(a["card"]), me, nobles, tgt_cost))
        card = by_id.get(best["card"])
        if card:
            if card["points"] >= 2:
                return best                       # 高分牌，直接拿
            if card["points"] == 1:
                if target is None or target[3] > 3 or bonus_total < 6 or my_tokens >= 8:
                    return best
            else:                                 # 0 分牌：只在前中期铺经济，且要能补目标色
                helps = tgt_cost and tgt_cost.get(card["bonus"], 0) > 0
                if bonus_total < 5 and my_tokens < 10 and helps:
                    return best

    takes = [a for a in legal if a["action"] == "take"]
    if my_tokens >= 10:
        takes = []                            # 已经满了，别再拿

    # 2. 攒宝石冲目标
    if takes and target is not None:
        _, card, need, gap = target
        if gap <= 6:
            best_take, best_val = None, -1e9
            for a in takes:
                gems = a["gems"]
                val = 0.0
                for g in gems:
                    if g in need:
                        val += 2.0 + need[g] * 0.5
                    else:
                        val -= 0.6
                if len(set(gems)) == 1 and gems[0] in need and need[gems[0]] >= 2:
                    val += 1.5
                overflow = my_tokens + len(gems) - 10
                if overflow > 0:
                    val -= overflow * 6.0     # 超过 10 枚会被强制弃置，很亏
                if val > best_val:
                    best_take, best_val = a, val
            if best_take is not None and best_val > 0:
                return best_take

    # 3. 攒不动就预约一张大牌（顺便拿黄金）
    reserves = [a for a in legal if a["action"] == "reserve"]
    if reserves and len(reserved) < 3:
        cand = [a for a in reserves
                if a.get("card") and by_id.get(a["card"], {}).get("points", 0) >= 4]
        if cand:
            return max(cand, key=lambda a: card_score(by_id[a["card"]], me, nobles))
        if not reserved:
            deck3 = [a for a in reserves if a.get("from") == "deck" and a.get("tier") == 3]
            if deck3 and bonus_total >= 3:
                return deck3[0]

    # 4. 兜底：拿最多种颜色
    if takes:
        need = target[2] if target else {}
        return max(takes, key=lambda a: sum(1 for g in set(a["gems"]) if g in need))
    return legal[0]


def main():
    recv()                              # init
    send({"v": 1, "type": "ready"})
    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            action = decide(msg.get("view") or {})
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": action})
        elif mtype in ("over", "shutdown"):
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
