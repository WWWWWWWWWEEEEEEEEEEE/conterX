#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · GLM 参赛选手

接入方式：独立进程（协议与 sdk/player_process_template.py 一致）。

策略概要（计划驱动 + 对抗细节）：
1. 选「计划卡」：场上可见卡/自己预约卡里，按 价值/完成转速 最划算的一张当目标；
2. 决策优先级：能买的高价值卡直接买（引擎复利 > 囤宝石）→ 有对手下回合要买走的
   高分卡就预约锁住（顺便拿金）→ 拿宝石精确填计划卡的缺口 → 兜底按需求拿色；
3. 快终盘（有人逼近 15 分 / 进入末轮）切换抢分模式，只认点数；
4. 超 10 枚宝石自己算好弃哪些（弃需求最低的色），显式写 return；
5. 只依赖 view 里的公开信息 + 自己 private，不看别人暗牌。

协议：init → ready，turn → action（原样回带 turn），over/shutdown → 退出。
"""

import json
import math
import sys

COLORS = ["white", "black", "red", "green", "blue"]


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


# ---------------------------------------------------------------------------
# 局面工具
# ---------------------------------------------------------------------------
def need_after_bonus(card, bon):
    """扣掉永久加成后每色还欠多少（>0 才记）。"""
    out = {}
    for c, v in (card["cost"] or {}).items():
        n = v - bon.get(c, 0)
        if n > 0:
            out[c] = n
    return out


def gap_of(card, bon, tok, gold):
    """买下这张卡还差几枚宝石（黄金可任意色，先抵扣）。"""
    need = need_after_bonus(card, bon)
    total = sum(max(0, n - tok.get(c, 0)) for c, n in need.items())
    return max(0, total - min(gold, total))


def opp_can_buy(card, opps):
    for p in opps:
        bon2, tok2, g2 = p.get("bonuses") or {}, p.get("tokens") or {}, p.get("gold", 0)
        total = 0
        for c, v in (card["cost"] or {}).items():
            n = v - bon2.get(c, 0)
            if n > 0:
                total += max(0, n - tok2.get(c, 0))
        if total <= g2:
            return True
    return False


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------
def decide(view):
    pub = view.get("public") or {}
    priv = view.get("private") or {}
    legal = priv.get("legal_actions") or []
    if not legal:
        return {"action": "pass"}

    pid = priv.get("player_id")
    players = pub.get("players") or []
    me = next((p for p in players if p.get("id") == pid), None)
    if me is None:
        return legal[0]

    nobles = pub.get("nobles") or []
    board = []
    for t in ("1", "2", "3"):
        for c in (pub.get("board") or {}).get(t) or []:
            if c:
                board.append(c)
    reserved = priv.get("reserved") or []
    visible = board + reserved
    by_id = {c["id"]: c for c in visible}

    tok = dict(me.get("tokens") or {})
    gold = me.get("gold", 0)
    bon = dict(me.get("bonuses") or {})
    my_cards = me.get("cards", 0)
    my_points = me.get("points", 0)
    bonus_total = sum(bon.values())
    token_limit = pub.get("token_limit", 10)
    hand_total = sum(tok.values()) + gold
    pool = pub.get("tokens") or {}
    pool_gold = pool.get("gold", 0)
    max_take = pub.get("max_take_colors", 3)
    target = pub.get("target_points", 15)
    final_round = bool(pub.get("final_round"))

    others = [p for p in players if p.get("id") != pid]
    lead = max((p.get("points", 0) for p in others), default=0)

    # 对手们对每色的需求：我也缺、对手也缺的颜色更值得抢（阻塞）
    opp_demand = {c: 0 for c in COLORS}
    for o in others:
        bon2 = o.get("bonuses") or {}
        for card in visible:
            for c, n in need_after_bonus(card, bon2).items():
                opp_demand[c] += n

    buys = [a for a in legal if a.get("action") == "buy"]
    takes = [a for a in legal if a.get("action") == "take"]
    resvs = [a for a in legal if a.get("action") == "reserve"]

    # 快终盘压力：末轮只认点数；有人到 target-2 也进入抢分模式
    urgent = final_round or (lead >= target - 2)
    # 色需求（用于估值与弃牌）：场上可见卡扣我加成后的缺口 + 近在眼前的贵族
    demand = {c: 0 for c in COLORS}
    for card in visible:
        for c, n in need_after_bonus(card, bon).items():
            demand[c] += n
    for n in nobles:
        req = n.get("requires") or {}
        prog = sum(min(bon.get(c, 0), v) for c, v in req.items())
        if prog >= sum(req.values()) - 2:            # 贵族近在眼前
            for c, v in req.items():
                demand[c] += v

    # 卡价值：分数 + 引擎（对缺口颜色的抵扣） + 贵族推进 + 便宜色优先
    pts_weight = 14.0 if urgent else 10.0

    def value(card):
        if card is None:
            return -1e9
        b = card["bonus"]
        s = card["points"] * pts_weight
        s += 0.75 * min(demand.get(b, 0), 14)
        for n in nobles:
            req = n.get("requires") or {}
            if b in req:
                more = max(0, req[b] - bon.get(b, 0))
                s += 3.5 if more <= 1 else 1.3
        s += (4 - card["tier"]) * 0.5
        # 黄金浪费惩罚：全靠金硬吃的贵卡不值
        gcost = gap_of(card, bon, tok, gold)
        s -= 0.4 * gcost
        return s

    # ------------------------------------------------------------------
    # 紧急抢分模式：末轮谁分高谁赢，只认点数
    # ------------------------------------------------------------------
    if urgent and buys:
        def pts_of(a):
            c = by_id.get(a.get("card"))
            return (c or {}).get("points", 0)
        best = max(buys, key=pts_of)
        if pts_of(best) >= 1 or final_round:
            return a_of(buys, best["card"])

    # ------------------------------------------------------------------
    # 1. 高价值可买卡直接买
    # ------------------------------------------------------------------
    if buys and not final_round:
        scored = [(value(by_id.get(a.get("card"))), a["card"]) for a in buys]
        scored.sort(reverse=True)
        bv, bid = scored[0]
        card = by_id.get(bid)
        worth = (card["points"] >= 2 or card["points"] >= 1 and bonus_total < 8
                 or card["points"] == 0 and bonus_total < 6
                 and demand.get(card["bonus"], 0) >= 3) or bv >= 5.0
        if worth:
            return a_of(buys, bid)

    # ------------------------------------------------------------------
    # 2. 计划卡：价值 / 转速 最划算的一张
    # ------------------------------------------------------------------
    plan, plan_gap = None, None
    best_plan_score = -1e9
    for card in visible:
        g = gap_of(card, bon, tok, gold)
        if g == 0:
            continue            # 能买的上面已处理
        turns = g / float(max_take)
        sc = value(card) - 3.0 * turns * (1.0 + 0.15 * turns)
        if sc > best_plan_score:
            best_plan_score, plan, plan_gap = sc, card, g

    plan_turns = math.ceil(plan_gap / float(max_take)) if plan else 0

    # 对手次日抢购卡：能锁就锁住
    contested = [(c, value(c)) for c in board if opp_can_buy(c, others)]
    contested.sort(key=lambda x: -x[1])

    # ------------------------------------------------------------------
    # 3. 防御性预约：对手下回合就能买走的卡
    # ------------------------------------------------------------------
    if resvs and len(reserved) < 3:
        for c, val in contested:
            if val >= 13.0 and value(by_id.get(c["id"], c)) >= 13.0:
                r = next((a for a in resvs if a.get("card") == c["id"]), None)
                if r is not None:
                    return r

    # ------------------------------------------------------------------
    # 4. 攒宝石冲计划卡（带溢出惩罚）
    # ------------------------------------------------------------------
    if takes and plan is not None:
        need = need_after_bonus(plan, bon)
        residual = {c: max(0, v - tok.get(c, 0)) for c, v in need.items()}
        # 黄金先抵
        covered = min(gold, sum(residual.values()))
        if covered:
            for c in sorted(residual, key=lambda x: -residual[x]):
                if covered <= 0:
                    break
                d = min(covered, residual[c])
                residual[c] -= d
                covered -= d

        best_take, best_val = None, -1e9
        for a in takes:
            gems = a.get("gems") or []
            val = 0.0
            for gcol in gems:
                if residual.get(gcol, 0) > 0:
                    val += 2.2 + 1.0 * residual[gcol]
                else:
                    val -= 0.9
                # 阻塞价值：这色对手们也缺，抢走让他们更慢
                val += 0.35 * min(opp_demand.get(gcol, 0), 8)
                # 拿到以后还能辅助其他卡（次级缺口）
                for c2 in visible:
                    gap2 = gap_of(c2, bon, tok, gold)
                    if gap2 > 0 and gcol in need_after_bonus(c2, bon):
                        val += 0.15
            overflow = hand_total + len(gems) - token_limit
            if overflow > 0:
                val -= overflow * 6.0
            if val > best_val:
                best_val, best_take = val, a
        if best_take is not None and best_val > 0.2:
            return best_take

    # ------------------------------------------------------------------
    # 5. 预约：锁住关键计划卡（顺便拿金） / 兜底按全场需求拿色
    # ------------------------------------------------------------------
    if resvs and len(reserved) < 3 and plan is not None:
        r = next((a for a in resvs if a.get("card") == plan["id"]), None)
        if r is not None and plan_turns >= 2 and value(plan) >= 11.0:
            return r
        rd = next((a for a in resvs if a.get("from") == "deck" and a.get("tier") == 3), None)
        if rd is not None and bonus_total <= 3 and plan_turns >= 4:
            return rd

    if takes:
        def take_val(a):
            gems = a.get("gems") or []
            s = 0.0
            for gcol in set(gems):
                if demand.get(gcol, 0) > 0:
                    s += 1.5 + 0.4 * min(demand[gcol], 10)
                else:
                    s -= 1.0
                s += 0.25 * min(opp_demand.get(gcol, 0), 8)
            s += 0.3 * len(set(gems))
            overflow = hand_total + len(gems) - token_limit
            if overflow > 0:
                s -= overflow * 6.0
            return s
        return max(takes, key=take_val)

    if resvs:
        return resvs[0]
    return legal[0]


def a_of(buys, card_id):
    for a in buys:
        if a.get("card") == card_id:
            return a
    return buys[0]


# 弃牌：拿宝石/预约后若超上限，显式给出需求最低颜色的弃置表
def pick_return(view, act):
    pub = view.get("public") or {}
    priv = view.get("private") or {}
    me = next((p for p in pub.get("players") or []
               if p.get("id") == priv.get("player_id")), None)
    if me is None:
        return None
    tok = dict(me.get("tokens") or {})
    gold = me.get("gold", 0)
    limit = pub.get("token_limit", 10)
    pool_gold = (pub.get("tokens") or {}).get("gold", 0)
    bon = dict(me.get("bonuses") or {})

    kind = act.get("action")
    if kind == "take":
        for g in act.get("gems") or []:
            tok[g] = tok.get(g, 0) + 1
    elif kind == "reserve" and pool_gold > 0:
        gold += 1
    else:
        return None
    excess = sum(tok.values()) + gold - limit
    if excess <= 0:
        return None
    # 需求最低（对全面板而言）的颜色先弃
    demand = {c: 0 for c in COLORS}
    for t in ("1", "2", "3"):
        for c in (pub.get("board") or {}).get(t) or []:
            if c:
                for col, n in need_after_bonus(c, bon).items():
                    demand[col] += n
    ret = {}
    for c in sorted(COLORS, key=lambda x: (demand.get(x, 0), tok.get(x, 0))):
        if excess <= 0:
            break
        take = min(excess, tok.get(c, 0))
        if take > 0:
            ret[c] = ret.get(c, 0) + take
            excess -= take
    return ret if excess == 0 else None


def main():
    recv()                                  # init
    send({"v": 1, "type": "ready"})
    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            action = decide(view)
            ret = pick_return(view, action)
            if ret:
                action = {**action, "return": ret}
            send({"v": 1, "type": "action",
                  "turn": msg.get("turn"), "action": action})
        elif mtype in ("over", "shutdown"):
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
