#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · Apex —— 参赛选手

只依赖 stdin/stdout 的 conterx/1 协议，不 import 中转站任何代码，也不碰其它文件。

思路：**给局面打分，而不是给动作打分**
--------------------------------------
1. 把 legal_actions 里的每个动作都模拟一遍，得到「执行后的局面」；
2. 对局面估值：分数 + 永久折扣 + 贵族进度 + 手上宝石的「可及性」；
   其中可及性 = 场上/预约里最值得买的几张牌，按「还差几颗宝石」折现——
   这就是「拿宝石」和「买牌」能在同一把尺子上比较的原因；
3. 选估值最高的动作，另加几条先验：抢对手能买的高分牌、别把黄金用光、终局只认分。

所有动作都来自 private.legal_actions，只在弃牌时补一个 return 字段，
所以不可能发出非法动作。
"""

import json
import os
import sys

COLORS = ("white", "black", "red", "green", "blue")
TIERS = ("1", "2", "3")

_DEBUG = bool(os.environ.get("APEX_DEBUG"))

W_POINT = 100.0         # 每分（目标 15 分，分数必须压倒一切）
W_BONUS = 40.0          # 每张永久折扣（约等于半分多，前期更值钱）
W_GOLD = 9.0            # 每枚黄金（万能，但只值半张牌）
W_NOBLE = 300.0         # 一张贵族 = 3 分
W_PLAN = 0.50           # 可及性折现系数
W_DENY = 46.0           # 抢走对手马上能买的高分牌
REACH = {0: 0.45, 1: 0.28, 2: 0.14, 3: 0.06, 4: 0.02}


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    line = line.strip()
    return json.loads(line) if line else {}


# ---------------------------------------------------------------------------
# 局面
# ---------------------------------------------------------------------------
class State:
    """一个「如果这样走，盘面会变成什么样」的快照。"""

    __slots__ = ("points", "bonuses", "tokens", "gold", "cards", "reserved", "pool")

    def __init__(self, points, bonuses, tokens, gold, cards, reserved, pool):
        self.points = points
        self.bonuses = bonuses
        self.tokens = tokens
        self.gold = gold
        self.cards = cards
        self.reserved = reserved      # 我预约的牌（仍可购买）
        self.pool = pool              # 场上明牌

    def copy(self):
        return State(self.points, dict(self.bonuses), dict(self.tokens), self.gold,
                     self.cards, list(self.reserved), list(self.pool))


class Ctx:
    """一回合的只读上下文。"""

    __slots__ = ("me", "nobles", "round", "final", "limit", "max_take", "target",
                 "opponents", "pool_gold", "nobles2")

    def __init__(self, view):
        pub = view.get("public") or {}
        priv = view.get("private") or {}
        pid = priv.get("player_id")
        self.me = None
        for p in pub.get("players") or []:
            if p.get("id") == pid:
                self.me = p
                break
        if self.me is None:
            self.me = {"tokens": {}, "bonuses": {}, "gold": 0, "points": 0, "cards": 0}
        self.nobles = pub.get("nobles") or []
        self.round = int(pub.get("round") or 1)
        self.final = bool(pub.get("final_round"))
        self.limit = int(pub.get("token_limit") or 10)
        self.max_take = int(pub.get("max_take_colors") or 3)
        self.target = int(pub.get("target_points") or 15)
        self.pool_gold = int((pub.get("tokens") or {}).get("gold") or 0)
        self.opponents = [p for p in (pub.get("players") or [])
                          if p.get("id") != self.me.get("id")]
        # 主线贵族：只盯最接近的 2 张，5 张一起算会把加成摊薄
        scored = []
        for n in self.nobles:
            req = n.get("requires") or {}
            remain = sum(max(0, v - (self.me.get("bonuses") or {}).get(k, 0))
                         for k, v in req.items())
            scored.append((remain, n))
        scored.sort(key=lambda x: x[0])
        self.nobles2 = [n for _, n in scored[:2]]


def gap_of(card, tokens, gold, bonuses):
    """还差几颗宝石才买得起（黄金可抵任意色）。"""
    need = 0
    for c in COLORS:
        v = (card.get("cost") or {}).get(c, 0) - bonuses.get(c, 0) - tokens.get(c, 0)
        if v > 0:
            need += v
    return max(0, need - gold)


def noble_hits(ctx, color):
    return sum(1 for n in ctx.nobles2 if color in (n.get("requires") or {}))


# ---------------------------------------------------------------------------
# 估值
# ---------------------------------------------------------------------------
def state_value(ctx, st):
    """给一个局面打分。所有动作最后都折算到这把尺子上比较。"""
    v = st.points * W_POINT
    v += sum(st.bonuses.values()) * W_BONUS
    v += min(st.gold, 3) * W_GOLD

    # 贵族：进度按平方放大（越接近越值钱），凑成直接给满
    for n in ctx.nobles:
        req = n.get("requires") or {}
        if not req:
            continue
        prog = min(1.0, min(st.bonuses.get(c, 0) / max(1, q) for c, q in req.items()))
        v += W_NOBLE * (prog ** 2)

    # 可及性：最值得买的三张牌，按「还差几颗」折现
    vals = []
    for card in st.pool + st.reserved:
        gap = gap_of(card, st.tokens, st.gold, st.bonuses)
        if gap > 4:
            continue
        base = int(card.get("points") or 0) * W_POINT
        base += W_BONUS + 7.0 * noble_hits(ctx, card.get("bonus"))
        vals.append(base * REACH.get(gap, 0.0))
    vals.sort(reverse=True)
    v += sum(vals[:3]) * W_PLAN

    # 手牌宝石本身有一点价值（资源），别让「什么都不做」和「拿 3 颗」打平
    v += sum(st.tokens.values()) * 1.2
    spare = sum(st.tokens.values()) + st.gold - ctx.limit
    if spare > 0:
        v -= spare * 12.0                     # 超过上限要弃牌，等于白拿
    return v


def opp_can_buy(ctx, opp, card):
    b = opp.get("bonuses") or {}
    t = opp.get("tokens") or {}
    g = int(opp.get("gold") or 0)
    return gap_of(card, t, g, b) == 0


# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------
def do_buy(ctx, st, card):
    nxt = st.copy()
    need = {}
    for c in COLORS:
        v = (card.get("cost") or {}).get(c, 0) - nxt.bonuses.get(c, 0)
        if v > 0:
            need[c] = v
    gold_used = 0
    for c, v in need.items():
        use = min(v, nxt.tokens.get(c, 0))
        nxt.tokens[c] = nxt.tokens.get(c, 0) - use
        gold_used += v - use
    nxt.gold = max(0, nxt.gold - gold_used)
    nxt.points += int(card.get("points") or 0)
    nxt.bonuses[card["bonus"]] = nxt.bonuses.get(card["bonus"], 0) + 1
    nxt.cards += 1
    nxt.pool = [c for c in nxt.pool if c["id"] != card["id"]]
    nxt.reserved = [c for c in nxt.reserved if c["id"] != card["id"]]
    return nxt


def do_take(ctx, st, gems):
    nxt = st.copy()
    for g in gems:
        nxt.tokens[g] = nxt.tokens.get(g, 0) + 1
    _trim(ctx, nxt)
    return nxt


def do_reserve(ctx, st, card):
    nxt = st.copy()
    nxt.reserved = list(nxt.reserved) + [card]
    nxt.pool = [c for c in nxt.pool if c["id"] != card["id"]]
    if ctx.pool_gold > 0:
        nxt.gold += 1
        _trim(ctx, nxt)
    return nxt


def _trim(ctx, st):
    """超上限时弃掉最没用的颜色（按当前目标的缺口排）。"""
    excess = sum(st.tokens.values()) + st.gold - ctx.limit
    if excess <= 0:
        return
    weight = {}
    for card in st.pool + st.reserved:
        for c in COLORS:
            v = (card.get("cost") or {}).get(c, 0) - st.bonuses.get(c, 0) - st.tokens.get(c, 0)
            if v > 0:
                weight[c] = weight.get(c, 0.0) + min(v, 3)
    for c in sorted(COLORS, key=lambda x: (weight.get(x, 0.0), -st.tokens.get(x, 0))):
        if excess <= 0:
            break
        drop = min(st.tokens.get(c, 0), excess)
        st.tokens[c] = st.tokens.get(c, 0) - drop
        excess -= drop


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def score_action(ctx, base, base_value, action, by_id):
    kind = action.get("action")
    st = None
    extra = 0.0

    if kind == "buy":
        card = by_id.get(action.get("card"))
        if not card:
            return None
        st = do_buy(ctx, base, card)
        pts = int(card.get("points") or 0)
        if pts and base.points + pts >= ctx.target:
            extra += 140.0                     # 这一手直接结束战斗
        if ctx.final:
            extra += pts * 60.0                # 终局轮：纯抢分
        # 把对手马上能买的牌拿走，等于废掉他一回合
        for o in ctx.opponents:
            if opp_can_buy(ctx, o, card):
                extra += W_DENY * 0.5
                if int(o.get("points") or 0) >= ctx.target - 3:
                    extra += 70.0
                break
    elif kind == "take":
        gems = action.get("gems") or []
        if not gems:
            return None
        st = do_take(ctx, base, gems)
        # 同色 ×2 只在真缺这个颜色时才好（它一回合只推进一种颜色）
        if len(set(gems)) == 1:
            extra -= 1.0
    elif kind == "reserve":
        card = by_id.get(action.get("card"))
        if card is None:
            # 盲抽牌堆：看不见牌面，只当「花一回合换 1 金 + 一张未知牌」
            st = base.copy()
            if ctx.pool_gold > 0:
                st.gold += 1
                _trim(ctx, st)
            extra -= 30.0
        else:
            st = do_reserve(ctx, base, card)
            pts = int(card.get("points") or 0)
            # 预约要花掉一整个回合，还占一个格子：默认先扣掉这份机会成本，
            # 只有「挡对手一张马上能买的高分牌」或者「留住自己两回合内能买的牌」才补回来。
            extra -= 34.0
            if pts >= 3 and any(opp_can_buy(ctx, o, card) for o in ctx.opponents):
                extra += W_DENY + 8.0 * pts
            if pts >= 2 and gap_of(card, base.tokens, base.gold, base.bonuses) <= 2:
                extra += 16.0
    elif kind == "pass":
        return base_value - 1.0
    else:
        return None

    if st is None:
        return None
    return state_value(ctx, st) + extra


def plan_return(ctx, base, action):
    """拿完会超上限时，明确告诉游戏弃哪些（否则它会从最多的颜色开始弃）。"""
    gems = action.get("gems") or []
    hand = dict(base.tokens)
    for g in gems:
        hand[g] = hand.get(g, 0) + 1
    if sum(hand.values()) + base.gold <= ctx.limit:
        return None
    # do_take 里已经按「最没用优先」弃过一轮，差额就是要告诉游戏的弃置清单
    st = do_take(ctx, base, gems)
    ret = {}
    for c in COLORS:
        d = hand.get(c, 0) - st.tokens.get(c, 0)
        if d > 0:
            ret[c] = d
    return ret or None


def decide(view):
    ctx = Ctx(view)
    priv = view.get("private") or {}
    legal = priv.get("legal_actions") or []
    if not legal:
        return {"action": "pass"}

    board = []
    pub = view.get("public") or {}
    for t in TIERS:
        for c in (pub.get("board") or {}).get(t) or []:
            if c:
                board.append(c)
    reserved = [c for c in (priv.get("reserved") or []) if c]
    by_id = {c["id"]: c for c in board + reserved}

    base = State(
        points=int(ctx.me.get("points") or 0),
        bonuses=dict(ctx.me.get("bonuses") or {}),
        tokens=dict(ctx.me.get("tokens") or {}),
        gold=int(ctx.me.get("gold") or 0),
        cards=int(ctx.me.get("cards") or 0),
        reserved=reserved,
        pool=board,
    )
    base_value = state_value(ctx, base)

    best, best_val = None, -1e18
    for a in legal:
        v = score_action(ctx, base, base_value, a, by_id)
        if v is not None and v > best_val:
            best, best_val = a, v
    if _DEBUG:
        sys.stderr.write(f"--- base={base_value:.0f} tokens={base.tokens} gold={base.gold}\n")
        for a in legal:
            v = score_action(ctx, base, base_value, a, by_id)
            sys.stderr.write(f"    {v if v is None else round(v):>7} {a}\n")

    if best is None:
        return legal[0]
    out = dict(best)
    if out.get("action") == "take":
        ret = plan_return(ctx, base, out)
        if ret:
            out["return"] = ret
    return out


# ---------------------------------------------------------------------------
def main():
    recv()                                  # init
    send({"v": 1, "type": "ready"})
    while True:
        msg = recv()
        if not msg:
            continue
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            try:
                action = decide(view)
            except Exception:               # 任何意外都不许让整局崩掉
                legal = ((view.get("private") or {}).get("legal_actions") or [])
                action = legal[0] if legal else {"action": "pass"}
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": action})
        elif mtype in ("over", "shutdown"):
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
