#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 · 基准玩家（baseline）

定位：一个「能正常跑、能正常决策」的基线程序，不强、但稳妥。
- 完全自包含：只读 stdin / 写 stdout 的 NDJSON，不 import 中转站任何代码；
- 决策只用软件在 view 里给的公开信息 + 自己的 private，不看别人暗牌；
- 直接在游戏给出的 private.legal_actions（全部合法动作）里挑，保证不会出非法动作；
- 策略是简单的贪心打分：能买分就买，差一点就攒对应颜色的宝石，早期顺手预约高分牌拿黄金。

协议三步：init → ready，turn → action（须原样回带 turn），over/shutdown → 退出。
"""

import json
import sys

COLORS = ["white", "black", "red", "green", "blue"]


def send(obj):
    """只往 stdout 写协议消息，一行一条 JSON。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    """从 stdin 读一行协议消息；遇到空行/坏行跳过，EOF 则退出。"""
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
# 工具：从 view 里提取信息
# ---------------------------------------------------------------------------
def find_me(public, player_id):
    for p in public.get("players") or []:
        if p.get("id") == player_id:
            return p
    return None


def collect_board(public):
    """把棋盘上三档明牌拍平成一个列表。"""
    out = []
    for t in ("1", "2", "3"):
        for c in (public.get("board") or {}).get(t) or []:
            if c:
                out.append(c)
    return out


def card_by_id(cards):
    return {c["id"]: c for c in cards if c}


def aggregate_want(me, cards, bonuses):
    """汇总我对各色宝石的「缺口」：把所有候选卡（棋盘 + 我的预约牌）扣掉已有加成
    和已有手牌后，还差多少。用来决定该去拿哪些颜色的宝石。"""
    want = {c: 0 for c in COLORS}
    tokens = me.get("tokens") or {}
    for c in cards:
        if not c:
            continue
        for color, v in (c.get("cost") or {}).items():
            if color not in COLORS:
                continue
            need = v - bonuses.get(color, 0) - tokens.get(color, 0)
            if need > 0:
                want[color] += need
    return want


# ---------------------------------------------------------------------------
# 给单个合法动作打分（越高越优先）
# ---------------------------------------------------------------------------
def score_buy(card, bonuses, nobles):
    if not card:
        return -999.0
    s = 50.0 + card["points"] * 20.0
    bonus = card["bonus"]
    # 新开一种加成色（永久收益）有额外吸引力
    if bonuses.get(bonus, 0) < 2:
        s += 3.0
    # 这张牌的加成色若正好是某张贵族需要的，且我离达标只差这一张，加分
    for n in nobles:
        req = n.get("requires") or {}
        if bonus in req and bonuses.get(bonus, 0) + 1 >= req[bonus]:
            s += 4.0
            break
    return s


def score_reserve(card, reserved_count):
    # 预约能顺手拿一枚黄金，且锁住高分牌；但已有 2~3 张预约时价值下降
    base = 28.0
    if card:
        base += card.get("points", 0) * 8.0
    if reserved_count >= 2:
        base -= 6.0
    return base


def score_take(action, want, my_tokens, token_limit):
    gems = action.get("gems") or []
    s = 0.0
    for g in gems:
        if g in want and want[g] > 0:
            s += 3.0 + want[g] * 0.5          # 拿我缺的颜色，越缺越值
        else:
            s -= 1.0                           # 拿不需要的颜色是浪费
    s += 0.5 * len(set(gems))                  # 多种不同色更灵活
    overflow = my_tokens + len(gems) - token_limit
    if overflow > 0:
        s -= overflow * 8.0                     # 超过上限会被强制弃置，很亏
    return s


# ---------------------------------------------------------------------------
# 主决策
# ---------------------------------------------------------------------------
def decide(view):
    public = view.get("public") or {}
    private = view.get("private") or {}
    legal = private.get("legal_actions") or []
    if not legal:
        return {"action": "pass"}

    me = find_me(public, private.get("player_id"))
    if me is None:
        return legal[0]                         # 兜底：照抄一个合法动作

    nobles = public.get("nobles") or []
    board = collect_board(public)
    reserved = private.get("reserved") or []
    by_id = card_by_id(board + reserved)
    bonuses = me.get("bonuses") or {}
    my_tokens = sum((me.get("tokens") or {}).values()) + me.get("gold", 0)
    token_limit = public.get("token_limit", 10)
    want = aggregate_want(me, board + reserved, bonuses)

    scored = []
    for a in legal:
        kind = a.get("action")
        if kind == "buy":
            scored.append((score_buy(by_id.get(a.get("card")), bonuses, nobles), a))
        elif kind == "reserve":
            c = by_id.get(a.get("card")) if a.get("card") else None  # 牌堆顶预约看不到牌
            scored.append((score_reserve(c, len(reserved)), a))
        elif kind == "take":
            scored.append((score_take(a, want, my_tokens, token_limit), a))
        else:  # pass 或其他
            scored.append((-1000.0, a))

    if not scored:
        return {"action": "pass"}
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def main():
    recv()                                      # init（本程序不依赖里面的字段）
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            turn = msg.get("turn")
            action = decide(msg.get("view") or {})
            send({"v": 1, "type": "action", "turn": turn, "action": action})
        elif mtype == "log":
            continue                            # 游戏/中转站的日志，忽略
        elif mtype in ("over", "shutdown"):
            break                               # 正常退出


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
