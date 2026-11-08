#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石规则自测（不依赖 conterX 引擎，直接驱动游戏类）

    python tests/test_splendor_rules.py

覆盖：设置、拿牌、预约、购买、宝石上限、贵族、终局轮、状态守恒。
"""

import importlib.util
import os
import random
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.join(BASE, "examples", "games", "splendor.py")

spec = importlib.util.spec_from_file_location("splendor", GAME)
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILED.append(name)


def mk(n=3, seed=1, cfg=None):
    players = [{"id": f"p{i+1}", "name": f"P{i+1}"} for i in range(n)]
    return sp.Splendor(players, seed, cfg or {})


def expect_illegal(fn, label):
    try:
        fn()
    except sp.Illegal:
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"       （{label} 抛了非 Illegal 异常：{exc!r}）")
        return False
    return False


# ---------------------------------------------------------------------------
def test_setup():
    print("\n[1] 设置")
    for n, per in ((2, 4), (3, 5), (4, 7), (5, 7)):
        g = mk(n)
        check(f"{n} 人局每种宝石 {per} 枚",
              all(g.tokens[c] == per for c in sp.COLORS), g.tokens)
        check(f"{n} 人局黄金 5 枚", g.gold == 5)
        check(f"{n} 人局贵族 {n + 1} 张", len(g.nobles) == n + 1, len(g.nobles))
        check(f"{n} 人局每级翻 4 张", all(len(g.board[t]) == 4 and all(g.board[t]) for t in (1, 2, 3)))
        check(f"{n} 人局牌堆 {40 - 4}/{30 - 4}/{20 - 4}",
              [len(g.decks[t]) for t in (1, 2, 3)] == [36, 26, 16],
              [len(g.decks[t]) for t in (1, 2, 3)])
    g5 = mk(5)
    check("5 人局每回合最多拿 2 种颜色", g5.max_take == 2, g5.max_take)
    g3 = mk(3)
    check("3 人局每回合可拿 3 种颜色", g3.max_take == 3, g3.max_take)


def test_take():
    print("\n[2] 拿取宝石")
    g = mk(3)
    p = g.players[0]
    g.apply(p, {"action": "take", "gems": ["white", "black", "red"]})
    check("拿 3 色各 1 枚", p.tokens["white"] == 1 and p.tokens["black"] == 1
          and p.tokens["red"] == 1, p.tokens)
    check("宝石池相应减少", g.tokens["white"] == 4 and g.tokens["red"] == 4)

    g.tokens["blue"] = 3
    ok = expect_illegal(lambda: g.apply(p, {"action": "take", "gems": ["blue", "blue"]}),
                        "同色剩 3 枚")
    check("同色剩 3 枚时不能拿 2 枚", ok)

    g.tokens["blue"] = 4
    g.apply(p, {"action": "take", "gems": ["blue", "blue"]})
    check("同色剩 4 枚时可以拿 2 枚", p.tokens["blue"] == 2, p.tokens)

    ok = expect_illegal(lambda: g.apply(p, {"action": "take", "gems": ["white", "black"]}),
                        "只拿 2 种不同色")
    check("3 人局不能只拿 2 种不同颜色", ok)

    g.tokens["green"] = 0
    ok = expect_illegal(lambda: g.apply(p, {"action": "take", "gems": ["green", "white", "red"]}),
                        "空色")
    check("不能拿已经空了的颜色", ok)


def test_reserve():
    print("\n[3] 预约")
    g = mk(3)
    p = g.players[0]
    card = g.board[2][0]
    g.apply(p, {"action": "reserve", "card": card["id"]})
    check("预约后进入自己的预约区", len(p.reserved) == 1 and p.reserved[0]["id"] == card["id"])
    check("预约同时拿 1 枚黄金", p.gold == 1 and g.gold == 4, (p.gold, g.gold))
    check("场上空位补新牌", all(g.board[2]))

    for _ in range(2):
        c = next(c for c in g.board[1] if c)
        g.apply(p, {"action": "reserve", "card": c["id"]})
    check("预约上限 3 张", len(p.reserved) == 3)
    c = next(c for c in g.board[3] if c)
    ok = expect_illegal(lambda: g.apply(p, {"action": "reserve", "card": c["id"]}), "第 4 张")
    check("第 4 张预约被拒绝", ok)

    g2 = mk(3)
    p2 = g2.players[0]
    deck_before = len(g2.decks[3])
    g2.apply(p2, {"action": "reserve", "tier": 3, "from": "deck"})
    check("可以从牌堆顶盲预约", len(p2.reserved) == 1 and len(g2.decks[3]) == deck_before - 1)

    g3 = mk(3)
    g3.gold = 0
    p3 = g3.players[0]
    c3 = g3.board[1][0]
    g3.apply(p3, {"action": "reserve", "card": c3["id"]})
    check("黄金空了仍可预约，只是拿不到黄金", p3.gold == 0 and len(p3.reserved) == 1)


def test_buy():
    print("\n[4] 购买")
    g = mk(3)
    p = g.players[0]
    # 挑一张 1 级卡，直接把宝石和加成配齐
    card = g.board[1][0]
    cost = card["cost"]
    for c, v in cost.items():
        p.tokens[c] = v
        g.tokens[c] -= v          # 模拟这些宝石确实是从池子里拿的
    pool_before = dict(g.tokens)
    g.apply(p, {"action": "buy", "card": card["id"]})
    check("买下后进入自己的发展卡", any(c["id"] == card["id"] for c in p.cards))
    check("获得对应颜色的永久加成", p.bonuses[card["bonus"]] == 1)
    check("宝石被付掉", all(p.tokens[c] == 0 for c in cost), p.tokens)
    check("宝石回到池子", all(g.tokens[c] == pool_before[c] + cost[c] for c in cost), g.tokens)
    check("场上空位补新牌", all(g.board[1]))

    # 加成抵费
    g2 = mk(3)
    p2 = g2.players[0]
    c2 = next(c for c in g2.board[1] if sum(c["cost"].values()) >= 3)
    main_color = max(c2["cost"], key=lambda k: c2["cost"][k])
    p2.bonuses[main_color] = c2["cost"][main_color]      # 加成刚好抵掉主色
    for c, v in c2["cost"].items():
        if c != main_color:
            p2.tokens[c] = v
    g2.apply(p2, {"action": "buy", "card": c2["id"]})
    check("永久加成可以抵扣费用", any(c["id"] == c2["id"] for c in p2.cards))

    # 黄金当万能
    g3 = mk(3)
    p3 = g3.players[0]
    c3 = g3.board[1][0]
    total = sum(c3["cost"].values())
    p3.gold = total
    g3.apply(p3, {"action": "buy", "card": c3["id"]})
    check("黄金可以替代任意颜色", any(c["id"] == c3["id"] for c in p3.cards) and p3.gold == 0)

    # 非法购买不能破坏状态（回归测试）
    g4 = mk(3)
    p4 = g4.players[0]
    c4 = g4.board[1][0]
    p4.tokens = {c: 0 for c in sp.COLORS}
    p4.gold = 0
    ok = expect_illegal(lambda: g4.apply(p4, {"action": "buy", "card": c4["id"]}), "买不起")
    check("买不起时报非法", ok)
    check("非法购买不会让场上的牌消失", g4._find_on_board(c4["id"]) is not None)

    # 预约牌可以被自己买走
    g5 = mk(3)
    p5 = g5.players[0]
    c5 = g5.board[2][0]
    g5.apply(p5, {"action": "reserve", "card": c5["id"]})
    for c, v in c5["cost"].items():
        p5.tokens[c] = v
    g5.apply(p5, {"action": "buy", "card": c5["id"]})
    check("可以购买自己预约的牌", any(c["id"] == c5["id"] for c in p5.cards)
          and len(p5.reserved) == 0)


def test_token_limit():
    print("\n[5] 宝石上限")
    g = mk(3)
    p = g.players[0]
    p.tokens = {c: 2 for c in sp.COLORS}          # 10 枚
    g.apply(p, {"action": "take", "gems": ["white", "black", "red"]})
    check("超过 10 枚自动弃到 10", p.token_total == 10, p.token_total)

    g2 = mk(3)
    p2 = g2.players[0]
    p2.tokens = {c: 0 for c in sp.COLORS}
    p2.tokens.update({"white": 4, "black": 4, "red": 2})
    g2.tokens["blue"] = 5
    g2.apply(p2, {"action": "take", "gems": ["blue", "blue"],
                  "return": {"white": 2}})
    check("可以指定弃哪些宝石", p2.token_total == 10, p2.token_total)


def test_return_validation():
    print("\n[10] return（弃置）字段校验")
    g = mk(3)
    p = g.players[0]
    p.tokens = {c: 2 for c in sp.COLORS}          # 手上 10 枚
    for c in sp.COLORS:                            # 这些宝石确实是从池子里拿的
        g.tokens[c] -= 2
    pool_before = dict(g.tokens)
    gems = ["white", "black", "red"]               # 拿完会到 13，需要弃 3

    def attempt(ret):
        return expect_illegal(
            lambda: g.apply(p, {"action": "take", "gems": gems, "return": ret}), "return")

    check("负数被拒", attempt({"white": -100}))
    check("负数不会污染宝石池", g.tokens == pool_before, g.tokens)
    check("负数不会改变手牌", p.token_total == 10, p.token_total)

    check("非整数被拒", attempt({"white": "abc"}))
    check("非整数不会白拿宝石", p.token_total == 10 and g.tokens == pool_before)

    check("超过手里数量被拒", attempt({"white": 99}))
    check("弃多了被拒", attempt({"white": 3, "black": 3}))
    check("未知颜色被拒", attempt({"purple": 1}))
    check("以上都未改变状态", p.token_total == 10 and g.tokens == pool_before)

    g.apply(p, {"action": "take", "gems": gems, "return": {"white": 3}})
    check("合法的 return 正常生效", p.token_total == 10, p.token_total)
    check("弃掉的宝石回到池子（池+手 恒等于初始 5 枚）",
          g.tokens["white"] + p.tokens["white"] == 5,
          (g.tokens["white"], p.tokens["white"]))


def test_nobles():
    print("\n[6] 贵族")
    g = mk(3)
    p = g.players[0]
    n = g.nobles[0]
    for c, v in n["requires"].items():
        p.bonuses[c] = v
    got = g._check_nobles(p)
    check("满足条件自动来访", len(p.nobles) == 1 and n["taken_by"] == p.pid, got)
    check("贵族值 3 分", p.points == 3, p.points)
    g._check_nobles(p)
    check("同一张贵族不会被重复拿走", len(p.nobles) == 1)


def test_end_round():
    print("\n[7] 终局轮")
    g = mk(3)
    turns = []

    def fake_send(msg):
        if msg.get("type") == "ask":
            turns.append(msg["turn"])
            fake_send.ask = msg

    def fake_recv():
        view = fake_send.ask["requests"][0]["view"]
        legal = view["private"]["legal_actions"]
        takes = [a for a in legal if a["action"] == "take"]
        action = (takes or legal)[0]
        # 第一位玩家第一次行动后，直接给他 15 分，触发终局
        if len(turns) == 1 and not g.players[0].cards:
            g.players[0].cards.append({"points": 15, "bonus": "white",
                                       "cost": {}, "id": "TEST", "tier": 1})
        pid = view["private"]["player_id"]
        return {"type": "actions", "actions": {pid: {"ok": True, "action": action}}}

    old_send, old_recv = sp.send, sp.recv
    sp.send, sp.recv = fake_send, fake_recv
    try:
        g.run()
    finally:
        sp.send, sp.recv = old_send, old_recv
    check("达到 15 分后本轮跑完即结束（3 人局共 3 手）", len(turns) == 3, turns)


def test_ranking():
    print("\n[8] 排名与平局")
    g = mk(3)
    a, b, c = g.players
    for p, pts, cards in ((a, 15, 12), (b, 15, 9), (c, 10, 14)):
        for _ in range(cards):
            p.cards.append({"points": 0, "bonus": "white", "cost": {}, "id": "x", "tier": 1})
        p.cards[0]["points"] = pts
    res = g._result()
    check("同分时发展卡少者排前", res["ranking"][0] == b.pid and res["ranking"][1] == a.pid,
          res["ranking"])
    check("分数原样输出", res["scores"][a.pid] == 15.0, res["scores"])


def test_conservation():
    print("\n[9] 随机对局的状态守恒（30 局）")
    rng = random.Random(0)
    bad_tokens = bad_cards = bad_points = bad_per_color = 0
    turns = []
    for seed in range(30):
        n = rng.choice([2, 3, 4])
        g = mk(n, seed)
        total_tokens = sum(g.tokens.values()) + g.gold
        turn = [0]

        def fake_send(msg):
            if msg.get("type") == "ask":
                fake_send.ask = msg

        def fake_recv():
            view = fake_send.ask["requests"][0]["view"]
            legal = view["private"]["legal_actions"]
            pid = view["private"]["player_id"]
            act = rng.choice(legal)
            turn[0] += 1
            return {"type": "actions", "actions": {pid: {"ok": True, "action": act}}}

        old_send, old_recv = sp.send, sp.recv
        sp.send, sp.recv = fake_send, fake_recv
        try:
            g.run()
        finally:
            sp.send, sp.recv = old_send, old_recv
        turns.append(turn[0])

        pool = sum(g.tokens.values()) + g.gold
        held = sum(p.token_total for p in g.players)
        if pool + held != total_tokens:
            bad_tokens += 1

        # 逐色核对：只查总数会漏掉「某色多了 1 枚、另一色少了 1 枚」这种污染
        per_color = dict(g.tokens)
        for p in g.players:
            for c in sp.COLORS:
                per_color[c] += p.tokens[c]
        per_color["gold"] = g.gold + sum(p.gold for p in g.players)
        want = {c: sp.TOKENS_BY_PLAYERS.get(n, 7) for c in sp.COLORS}
        want["gold"] = 5
        if per_color != want:
            bad_per_color += 1

        all_cards = sum(len(g.decks[t]) for t in (1, 2, 3))
        all_cards += sum(1 for t in (1, 2, 3) for c in g.board[t] if c)
        all_cards += sum(len(p.cards) + len(p.reserved) for p in g.players)
        if all_cards != 90:
            bad_cards += 1

        for p in g.players:
            expect = sum(c["points"] for c in p.cards) + 3 * len(p.nobles)
            if p.points != expect:
                bad_points += 1

    check("宝石总量守恒", bad_tokens == 0, f"{bad_tokens} 局异常")
    check("卡牌总数守恒（90 张）", bad_cards == 0, f"{bad_cards} 局异常")
    check("分数等于卡牌+贵族之和", bad_points == 0, f"{bad_points} 局异常")
    check("每局都能正常结束", all(t > 0 for t in turns), turns[:5])
    check("每种颜色的宝石数量守恒（不只是总数）", bad_per_color == 0,
          f"{bad_per_color} 局异常")
    print(f"       平均手数 {sum(turns) / len(turns):.1f}，最长 {max(turns)}")


if __name__ == "__main__":
    test_setup()
    test_take()
    test_reserve()
    test_buy()
    test_token_limit()
    test_return_validation()
    test_nobles()
    test_end_round()
    test_ranking()
    test_conservation()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全部通过")
