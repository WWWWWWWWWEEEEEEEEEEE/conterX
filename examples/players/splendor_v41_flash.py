#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · 参赛选手「v41-flash」

策略
----
1. 不实现规则、不依赖中转站代码，只读 turn 里的 view；
2. 永远从 private.legal_actions 里选动作，保证不产生非法动作；
3. 对每个合法动作做一次状态模拟（付款/拿宝石/预约/超上限弃牌），
   用「分数 + 永久加成 + 贵族进度 + 目标牌可及性」打分；
4. 目标牌按 分数/加成/缺口 折现，能买就买，买不起就拿最接近目标的宝石；
5. 中期会为了高价值牌预约并拿黄金，也会抢/锁对手马上能买的高分牌；
6. 临近终局提高分数权重，优先把分拿到手。

协议：init -> ready，turn -> action（原样回带 turn），over/shutdown -> 退出。
"""

import json
import sys

COLORS = ["white", "black", "red", "green", "blue"]
TIERS = ("1", "2", "3")

# ---- 权重（经批量对局校准） ------------------------------------------------
W_POINT = 100.0          # 每分
W_BONUS = 40.0           # 每张永久加成的基础价值
W_OWN = 10.0             # 已拥有永久加成的底价（另加需求颜色溢价）
W_NOBLE = 300.0          # 完成一张贵族（3 分）的价值上限
NOBLE_MODE = 0           # 0=按总缺口平滑进度，1=按最短板颜色算进度
NOBLE_TOPN = 2           # 只重点盯最接近的 N 张贵族，0=全部
W_PLAN = 0.50            # 目标牌可及性折现
W_GOLD = 9.0             # 手上一枚黄金的额外价值（灵活性）
W_DENY = 55.0            # 抢走对手能买的高分牌
W_RESERVE = 0.06         # 预约牌「已经锁定」的折扣价值
W_END = 1.55             # 终局分数权重倍率
USE_SMART_TRIM = 1.0     # 超上限时主动选择弃牌颜色


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
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
# view 解析
# ---------------------------------------------------------------------------
def find_me(public, pid):
    for p in public.get("players") or []:
        if p.get("id") == pid:
            return p
    return None


def flatten_board(public):
    out = []
    for t in TIERS:
        for card in (public.get("board") or {}).get(t) or []:
            if card:
                out.append(card)
    return out


def tokens_of(player):
    t = dict(player.get("tokens") or {})
    for c in COLORS:
        t[c] = int(t.get(c, 0) or 0)
    return t


def bonuses_of(player):
    b = dict(player.get("bonuses") or {})
    for c in COLORS:
        b[c] = int(b.get(c, 0) or 0)
    return b


# ---------------------------------------------------------------------------
# 估值
# ---------------------------------------------------------------------------
def attraction(card, ctx):
    """一张牌本身有多好（不含“还差多少宝石”）。"""
    if not card:
        return 0.0
    pts = int(card.get("points", 0) or 0)
    bonus = card.get("bonus")
    noble_hits = sum(1 for n in ctx["nobles"] if bonus in (n.get("requires") or {}))
    # 这张牌的加成色正好补齐多少张目标牌的费用
    needers = 0
    for c in ctx["cards"]:
        if c.get("cost", {}).get(bonus, 0) > 0:
            needers += 1
    s = pts * (W_POINT + 10.0)
    s += W_BONUS + 6.0 * noble_hits + 1.6 * min(needers, 6)
    s += 2.0 * (int(card.get("tier", 1) or 1) - 1)
    return s


def gap_of(card, tokens, gold, bonuses):
    """还差多少枚宝石（黄金可顶任意色）。"""
    need = 0
    for c in COLORS:
        need += max(0, int(card.get("cost", {}).get(c, 0) or 0) - bonuses.get(c, 0) - tokens.get(c, 0))
    return max(0, need - gold)


def noble_value(ctx, bonuses):
    scored = []
    for n in ctx["nobles"]:
        req = n.get("requires") or {}
        tot = sum(int(v) for v in req.values())
        if tot <= 0:
            continue
        need = sum(max(0, int(v) - bonuses.get(c, 0)) for c, v in req.items())
        if NOBLE_MODE:
            prog = min(1.0, min(bonuses.get(c, 0) / max(1, int(v))
                                for c, v in req.items()))
        else:
            prog = max(0.0, 1.0 - need / tot)
        scored.append((prog, n))
    scored.sort(key=lambda x: -x[0])
    if NOBLE_TOPN > 0:
        scored = scored[:int(NOBLE_TOPN)]
    return sum(W_NOBLE * (prog ** 2) for prog, _ in scored)


def bonus_value(ctx, bonuses):
    # 永久加成本身就是经济：越是被目标牌需要的颜色越值钱
    dem = {c: 0 for c in COLORS}
    for card in ctx["cards"]:
        for c, v in (card.get("cost") or {}).items():
            if c in dem:
                dem[c] += int(v)
    s = 0.0
    for c in COLORS:
        s += bonuses.get(c, 0) * (W_OWN + 1.2 * min(dem[c], 8))
    return s + noble_value(ctx, bonuses)


def plan_value(ctx, tokens, gold, bonuses, cards):
    """目标牌可及性：越接近买到、牌越好，折现值越高。"""
    vals = []
    for card in cards:
        a = attraction(card, ctx)
        if a <= 0:
            continue
        g = gap_of(card, tokens, gold, bonuses)
        # 每差一枚宝石，折现下降；差 0 枚就是“马上能买”
        vals.append(a / (1.0 + 0.45 * g))
    vals.sort(reverse=True)
    return sum(vals[:3]) * W_PLAN


def state_value(ctx, pts, tokens, gold, bonuses, cards, reserved):
    v = pts * ctx["point_weight"]
    v += bonus_value(ctx, bonuses)
    v += plan_value(ctx, tokens, gold, bonuses, cards)
    # 少量灵活性价值：完全没用的同色宝石也会占手牌，所以很低
    v += 1.2 * sum(min(tokens[c], 3) for c in COLORS)
    v += W_GOLD * min(gold, 3)
    v += sum(W_RESERVE * attraction(c, ctx) for c in reserved)
    # 同分时发展卡少者胜：轻微惩罚牌数
    v -= 2.0 * max(0, int(ctx["my_cards"]) - 6)
    return v


# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------
def pay_for(card, tokens, gold, bonuses):
    """与游戏一致的支付方式：先用手牌，缺口用黄金。"""
    need = {}
    for c in COLORS:
        v = int(card.get("cost", {}).get(c, 0) or 0) - bonuses.get(c, 0)
        if v > 0:
            need[c] = v
    new_tokens = dict(tokens)
    used_gold = 0
    for c, v in need.items():
        use = min(v, new_tokens.get(c, 0))
        new_tokens[c] = new_tokens.get(c, 0) - use
        used_gold += v - use
    if used_gold > gold:
        return None
    return new_tokens, gold - used_gold


def trim_tokens(tokens, gold, limit):
    """按游戏 `_plan_trim` 的默认规则（从最多的颜色开始弃，黄金最后）模拟。"""
    tokens = dict(tokens)
    excess = sum(tokens.values()) + gold - limit
    if excess <= 0:
        return tokens, gold
    while excess > 0:
        cand = [(c, tokens.get(c, 0)) for c in COLORS if tokens.get(c, 0) > 0]
        if cand:
            c, _ = max(cand, key=lambda kv: kv[1])
            tokens[c] -= 1
        elif gold > 0:
            gold -= 1
        else:
            break
        excess -= 1
    return tokens, gold


def useful_colors(ctx, tokens, gold, bonuses):
    """当前手牌下，各颜色对「最近能买到的牌」的边际价值。"""
    w = {c: 0.0 for c in COLORS}
    for card in ctx["cards"]:
        gap = gap_of(card, tokens, gold, bonuses)
        if gap > 4:
            continue
        base = int(card.get("points", 0) or 0) * 100.0 + 40.0
        for c in COLORS:
            d = int(card.get("cost", {}).get(c, 0) or 0) - bonuses.get(c, 0) - tokens.get(c, 0)
            if d > 0:
                w[c] += min(d, 3) * base / (1.0 + 0.45 * gap)
    return w


def smart_trim(tokens, gold, limit, weights):
    """超上限时主动弃「最没用」的颜色，而不是让游戏默认弃最多的一种。

    返回 (新宝石, 新黄金, return 字段或 None)。
    """
    excess = sum(tokens.values()) + gold - limit
    if excess <= 0:
        return dict(tokens), gold, None
    nt = dict(tokens)
    ret = {}
    for c in sorted(COLORS, key=lambda x: (weights.get(x, 0.0), -nt.get(x, 0))):
        if excess <= 0:
            break
        drop = min(nt.get(c, 0), excess)
        if drop > 0:
            nt[c] -= drop
            ret[c] = drop
            excess -= drop
    ng = gold
    if excess > 0 and gold > 0:
        drop = min(gold, excess)
        ng -= drop
        ret["gold"] = drop
        excess -= drop
    if excess > 0:                                  # 理论上不会发生，保守退回默认规则
        return trim_tokens(dict(tokens), gold, limit)[0], \
               trim_tokens(dict(tokens), gold, limit)[1], None
    return nt, ng, (ret or None)


def opponent_can_buy(card, ctx):
    for opp in ctx["opponents"]:
        if gap_of(card, tokens_of(opp), int(opp.get("gold", 0) or 0),
                  bonuses_of(opp)) == 0:
            return True
    return False


def opponent_near_buy(card, ctx, slack=2):
    for opp in ctx["opponents"]:
        if gap_of(card, tokens_of(opp), int(opp.get("gold", 0) or 0),
                  bonuses_of(opp)) <= slack:
            return True
    return False


# ---------------------------------------------------------------------------
# 动作打分
# ---------------------------------------------------------------------------
def score_action(action, ctx):
    kind = action.get("action")
    me = ctx["me"]
    tokens = ctx["tokens"]
    gold = ctx["gold"]
    bonuses = ctx["bonuses"]
    limit = ctx["token_limit"]

    if kind == "buy":
        card = ctx["by_id"].get(action.get("card"))
        if not card:
            return -1e9
        paid = pay_for(card, tokens, gold, bonuses)
        if paid is None:
            return -1e9
        nt, ng = paid
        nb = dict(bonuses)
        nb[card.get("bonus")] = nb.get(card.get("bonus"), 0) + 1
        cards = [c for c in ctx["cards"] if c.get("id") != card["id"]]
        npts = ctx["points"] + int(card.get("points", 0) or 0)
        v = state_value(ctx, npts, nt, ng, nb, cards, ctx["reserved"])
        v += 18.0                                   # 买牌本身是推进节奏的
        deny = 0.0
        if opponent_can_buy(card, ctx):
            deny = W_DENY + 12.0 * int(card.get("points", 0) or 0)
        elif opponent_near_buy(card, ctx, 2):
            deny = W_DENY * 0.35
        v += deny
        if npts >= ctx["target"] and npts > ctx["leader_points"]:
            v += 90.0                               # 先到终点，逼别人追分
        return v

    if kind == "take":
        gems = list(action.get("gems") or [])
        nt = dict(tokens)
        for g in gems:
            if g in nt:
                nt[g] += 1
        w = useful_colors(ctx, nt, gold, bonuses)
        if USE_SMART_TRIM:
            nt, ng, _ret = smart_trim(nt, gold, limit, w)
        else:
            nt, ng = trim_tokens(nt, gold, limit)
        overflow = max(0, sum(tokens.values()) + len(gems) + gold - limit)
        v = state_value(ctx, ctx["points"], nt, ng, bonuses, ctx["cards"], ctx["reserved"])
        # 拿完后完全用不上的颜色：小幅扣分，避免无意义囤积
        used = set()
        for card in ctx["cards"]:
            for c in COLORS:
                if int(card.get("cost", {}).get(c, 0) or 0) > bonuses.get(c, 0) + nt.get(c, 0):
                    used.add(c)
        junk = sum(1 for g in gems if g not in used)
        v -= 9.0 * junk
        v -= 26.0 * overflow
        # 拿牌是“保底”动作，不要因为买不起就什么都不做
        v += 4.0 * min(3, len(set(gems)))
        return v

    if kind == "reserve":
        cid = action.get("card")
        card = ctx["by_id"].get(cid) if cid else None
        if card is None:
            # 盲抽牌堆拿不到牌面信息，基本是拿 1 金换一次节奏，价值很低
            return -1e7
        # 预约必须克制：只锁「马上够得着」的高分牌，或抢对手马上能买的牌
        a = attraction(card, ctx)
        g = gap_of(card, tokens, gold, bonuses)
        deny_now = opponent_can_buy(card, ctx)
        if len(ctx["reserved"]) >= 2:
            return -1e7
        if not deny_now and (g > 5 or a < 200.0):
            return -1e7
        ng = gold + (1 if ctx["gold_pool"] > 0 else 0)
        w = useful_colors(ctx, tokens, ng, bonuses)
        if USE_SMART_TRIM:
            nt, ng, _ret = smart_trim(dict(tokens), ng, limit, w)
        else:
            nt, ng = trim_tokens(dict(tokens), ng, limit)
        reserved = list(ctx["reserved"]) + [card]
        cards = [c for c in ctx["cards"] if c.get("id") != card["id"]]
        v = state_value(ctx, ctx["points"], nt, ng, bonuses, cards, reserved)
        v += 8.0 if deny_now else 0.0
        if opponent_near_buy(card, ctx, 2):
            v += W_DENY * 0.25
        # 已经有一张预约时，再预约的边际价值下降
        v -= 45.0 * len(ctx["reserved"])
        return v

    return -1e8


# ---------------------------------------------------------------------------
# 主决策
# ---------------------------------------------------------------------------
def build_ctx(public, private, me, pid):
    board = flatten_board(public)
    reserved = [c for c in (private.get("reserved") or []) if c]
    by_id = {c["id"]: c for c in board + reserved}
    target = int(public.get("target_points", 15) or 15)
    final_round = bool(public.get("final_round"))
    opps = [p for p in (public.get("players") or []) if p.get("id") != pid]
    leader = max([int(p.get("points", 0) or 0) for p in opps] + [0])
    my_pts = int(me.get("points", 0) or 0)

    # 终局：分数就是一切；常规局：分数略高于经济
    point_weight = W_POINT
    if final_round or leader >= target - 3 or my_pts >= target - 4:
        point_weight = W_POINT * W_END

    return {
        "me": me,
        "pid": pid,
        "opponents": opps,
        "points": my_pts,
        "tokens": tokens_of(me),
        "gold": int(me.get("gold", 0) or 0),
        "bonuses": bonuses_of(me),
        "my_cards": int(me.get("cards", 0) or 0),
        "cards": board + reserved,
        "board": board,
        "reserved": reserved,
        "by_id": by_id,
        "nobles": public.get("nobles") or [],
        "target": target,
        "leader_points": leader,
        "final_round": final_round,
        "token_limit": int(public.get("token_limit", 10) or 10),
        "gold_pool": int((public.get("tokens") or {}).get("gold", 0) or 0),
        "point_weight": point_weight,
    }


def attach_return(action, ctx):
    """为 take / reserve 补一个更聪明的弃牌清单（不超上限时原样返回）。"""
    kind = action.get("action")
    if not USE_SMART_TRIM:
        return action
    tokens = ctx["tokens"]
    gold = ctx["gold"]
    limit = ctx["token_limit"]
    bonuses = ctx["bonuses"]
    if kind == "take":
        hand = dict(tokens)
        for g in (action.get("gems") or []):
            if g in hand:
                hand[g] += 1
        w = useful_colors(ctx, hand, gold, bonuses)
        _nt, _ng, ret = smart_trim(hand, gold, limit, w)
    elif kind == "reserve":
        ngold = gold + (1 if ctx["gold_pool"] > 0 else 0)
        w = useful_colors(ctx, tokens, ngold, bonuses)
        _nt, _ng, ret = smart_trim(dict(tokens), ngold, limit, w)
    else:
        return action
    if not ret:
        return action
    out = dict(action)
    out["return"] = ret
    return out


def _decide(view):
    public = view.get("public") or {}
    private = view.get("private") or {}
    legal = private.get("legal_actions") or []
    if not legal:
        return {"action": "pass"}

    pid = private.get("player_id")
    me = find_me(public, pid)
    if me is None:                                   # 视野异常时照抄一个合法动作
        return legal[0]

    ctx = build_ctx(public, private, me, pid)
    best_action, best_score = None, -1e18
    for a in legal:
        try:
            s = score_action(a, ctx)
        except Exception:
            s = -1e8                               # 单条动作估值异常不影响其他动作
        if s > best_score:
            best_score, best_action = s, a
    if best_action is None:
        return legal[0]
    try:
        return attach_return(best_action, ctx)
    except Exception:
        return best_action


def decide(view):
    """任何意外都退回「照抄一个合法动作」，绝不让进程因为估值出错而崩掉。"""
    try:
        return _decide(view)
    except Exception:
        try:
            legal = (view.get("private") or {}).get("legal_actions") or []
            return legal[0] if legal else {"action": "pass"}
        except Exception:
            return {"action": "pass"}


def main():
    init = recv()
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            turn = msg.get("turn")
            action = decide(msg.get("view") or {})
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
