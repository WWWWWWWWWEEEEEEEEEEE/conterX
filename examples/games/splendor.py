#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""璀璨宝石 Splendor —— conterX 进程式上游游戏

支持 2~5 人（5 人局为官方变体：宝石数量不变，但拿取指示物只能拿 2 种不同颜色）。

规则要点
--------
设置：每级发展卡洗混、各翻 4 张；宝石按人数配（2人4枚/3人5枚/4人5人7枚），黄金恒 5 枚；
      贵族 = 人数 + 1 张（随机抽取）。
回合：四选一
      1. 拿 3 枚不同色（5 人局为 2 枚不同色）
      2. 拿 2 枚同色（该色剩余 ≥ 4 才可）
      3. 预约一张卡（明牌或牌堆顶），同时拿 1 枚黄金；预约上限 3 张
      4. 购买一张卡（场上或自己的预约牌），黄金可当任意色
结束：回合结束时手牌宝石 > 10 要弃到 10；满足贵族条件立刻来访（3 分，不占行动）。
      有人达到 15 分后，本轮打完再比大小；同分者发展卡数量少者胜。

动作格式（发回 {"type":"action","action":{...}}）
--------------------------------------------------
{"action": "take",    "gems": ["white","blue","red"]}   或 ["blue","blue"]
{"action": "take",    "gems": [...], "return": {"white": 1}}   ← 可选，超 10 枚时指定弃哪些
{"action": "reserve", "card": "L2-07"}                   ← 预约场上明牌
{"action": "reserve", "tier": 3, "from": "deck"}         ← 预约牌堆顶
{"action": "buy",     "card": "L2-07"}                   ← 买场上明牌或自己的预约牌
{"action": "pass"}                                       ← 仅在无合法动作时允许

视野（view）里会附带 private.legal_actions 合法动作清单，照抄其中一个即为合法动作。
"""

import itertools
import json
import os
import random
import sys

V = 1
HERE = os.path.dirname(os.path.abspath(__file__))
CARDS_PATH = os.path.join(HERE, "splendor_cards.json")

COLORS = ["white", "black", "red", "green", "blue"]
CN = {"white": "白", "black": "黑", "red": "红", "green": "绿", "blue": "蓝", "gold": "金"}
CN_FULL = {"white": "钻石", "black": "玛瑙", "red": "红宝石", "green": "翡翠", "blue": "蓝宝石"}

# 每种宝石的指示物数量（按人数）；5 人局沿用 4 人局的 7 枚
TOKENS_BY_PLAYERS = {2: 4, 3: 5, 4: 7, 5: 7}
GOLD_TOTAL = 5
TOKEN_LIMIT = 10


# ---------------------------------------------------------------------------
# 通信
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


def log(text, level="info"):
    for one in str(text).split("\n"):
        if one.strip():
            send({"v": V, "type": "log", "text": one, "level": level})


class Illegal(Exception):
    """动作非法。"""


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_cards():
    if not os.path.exists(CARDS_PATH):
        raise FileNotFoundError(f"找不到卡表文件：{CARDS_PATH}（它需要和 splendor.py 放在同一目录）")
    with open(CARDS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    cards, nobles = data.get("cards"), data.get("nobles")
    if not cards or len(cards) != 90:
        raise ValueError(f"卡表应包含 90 张发展卡，实际 {len(cards) if cards else 0} 张")
    return cards, nobles or []


def fmt_cost(cost):
    if not cost:
        return "免费"
    return "".join(f"{CN.get(k, k)}{v}" for k, v in cost.items() if v)


def fmt_gems(d):
    if not d:
        return "无"
    return "".join(f"{CN.get(k, k)}{v}" for k, v in d.items() if v)


# ---------------------------------------------------------------------------
# 玩家状态
# ---------------------------------------------------------------------------
class PlayerState:
    def __init__(self, pid, name, seat):
        self.pid = pid
        self.name = name
        self.seat = seat
        self.tokens = {c: 0 for c in COLORS}
        self.gold = 0
        self.bonuses = {c: 0 for c in COLORS}
        self.cards = []
        self.reserved = []
        self.nobles = []
        self.illegal = 0

    @property
    def points(self):
        return sum(c["points"] for c in self.cards) + 3 * len(self.nobles)

    @property
    def token_total(self):
        return sum(self.tokens.values()) + self.gold

    def public(self):
        return {
            "id": self.pid,
            "name": self.name,
            "points": self.points,
            "tokens": dict(self.tokens),
            "gold": self.gold,
            "bonuses": dict(self.bonuses),
            "cards": len(self.cards),
            "reserved_count": len(self.reserved),
            "nobles": [n["id"] for n in self.nobles],
        }


# ---------------------------------------------------------------------------
# 游戏
# ---------------------------------------------------------------------------
class Splendor:
    name = "璀璨宝石"

    def __init__(self, players, seed, config):
        self.cfg = config or {}
        self.rng = random.Random(seed)
        self.players = [PlayerState(p["id"], p.get("name", p["id"]), i)
                        for i, p in enumerate(players)]
        self.n = len(self.players)
        self.target = int(self.cfg.get("target_points", 15))
        self.token_limit = int(self.cfg.get("token_limit", TOKEN_LIMIT))
        self.verbose = bool(self.cfg.get("verbose", False))
        self.deadline_ms = int(self.cfg.get("deadline_ms", 3000))

        # 5 人局：拿取指示物只能拿 2 种不同颜色
        default_take = 2 if self.n >= 5 else 3
        self.max_take = int(self.cfg.get("max_take_colors", default_take))

        self.cards, all_nobles = load_cards()
        self.decks = {1: [], 2: [], 3: []}
        for c in self.cards:
            self.decks[c["tier"]].append(c)
        for t in self.decks:
            self.rng.shuffle(self.decks[t])

        self.board = {t: [] for t in (1, 2, 3)}
        for t in (1, 2, 3):
            for _ in range(4):
                self._refill(t)

        per_color = TOKENS_BY_PLAYERS.get(self.n, 7)
        self.tokens = {c: per_color for c in COLORS}
        self.gold = GOLD_TOTAL

        n_nobles = min(self.n + 1, len(all_nobles))
        chosen = self.rng.sample(all_nobles, n_nobles)
        self.nobles = [dict(x, taken_by=None) for x in chosen]

        self.turn_no = 0
        self.round = 1
        self.end_triggered = False
        self.card_index = {}
        for c in self.cards:
            self.card_index[c["id"]] = c

    # -- 发牌 --------------------------------------------------------------
    def _refill(self, tier):
        slot = self.board[tier]
        if len(slot) < 4:
            if self.decks[tier]:
                slot.append(self.decks[tier].pop())
            else:
                slot.append(None)
        else:
            slot.append(None)

    def _take_from_board(self, card_id):
        for t in (1, 2, 3):
            for i, c in enumerate(self.board[t]):
                if c and c["id"] == card_id:
                    self.board[t][i] = None
                    self._fill_hole(t)
                    return c
        return None

    def _fill_hole(self, tier):
        slot = self.board[tier]
        for i in range(len(slot)):
            if slot[i] is None:
                slot[i] = self.decks[tier].pop() if self.decks[tier] else None
                return

    # -- 工具 --------------------------------------------------------------
    @staticmethod
    def card_public(c):
        if not c:
            return None
        return {
            "id": c["id"],
            "tier": c["tier"],
            "points": c["points"],
            "bonus": c["bonus"],
            "cost": dict(c["cost"]),
        }

    def need_after_bonus(self, p, card):
        need = {}
        for c in COLORS:
            v = card["cost"].get(c, 0) - p.bonuses[c]
            if v > 0:
                need[c] = v
        return need

    def affordable(self, p, card):
        need = self.need_after_bonus(p, card)
        short = 0
        for c, v in need.items():
            short += max(0, v - p.tokens[c])
        return short <= p.gold

    def payment(self, p, card):
        """返回 (每种宝石付出的数量, 用掉几个黄金)。"""
        need = self.need_after_bonus(p, card)
        pay, gold = {}, 0
        for c, v in need.items():
            use = min(v, p.tokens[c])
            if use:
                pay[c] = use
            gold += v - use
        return pay, gold

    # -- 合法动作 ----------------------------------------------------------
    def legal_actions(self, p):
        acts = []
        # 购买
        for t in (1, 2, 3):
            for c in self.board[t]:
                if c and self.affordable(p, c):
                    acts.append({"action": "buy", "card": c["id"]})
        for c in p.reserved:
            if self.affordable(p, c):
                acts.append({"action": "buy", "card": c["id"]})
        # 预约
        if len(p.reserved) < 3:
            for t in (1, 2, 3):
                for c in self.board[t]:
                    if c:
                        acts.append({"action": "reserve", "card": c["id"]})
                if self.decks[t]:
                    acts.append({"action": "reserve", "tier": t, "from": "deck"})
        # 拿宝石
        avail = [c for c in COLORS if self.tokens[c] > 0]
        if avail:
            k = min(self.max_take, len(avail))
            if k >= 1:
                for combo in itertools.combinations(avail, k):
                    acts.append({"action": "take", "gems": list(combo)})
        for c in COLORS:
            if self.tokens[c] >= 4:
                acts.append({"action": "take", "gems": [c, c]})
        if not acts:
            acts.append({"action": "pass"})
        return acts

    # -- 视野 --------------------------------------------------------------
    def build_view(self, p):
        public = {
            "turn": self.turn_no,
            "round": self.round,
            "current": p.pid,
            "target_points": self.target,
            "final_round": self.end_triggered,
            "board": {str(t): [self.card_public(c) for c in self.board[t]] for t in (1, 2, 3)},
            "decks": {str(t): len(self.decks[t]) for t in (1, 2, 3)},
            "tokens": dict(self.tokens, gold=self.gold),
            "nobles": [
                {"id": x["id"], "points": x["points"], "requires": dict(x["requires"])}
                for x in self.nobles if x["taken_by"] is None
            ],
            "players": [q.public() for q in self.players],
            "token_limit": self.token_limit,
            "max_take_colors": self.max_take,
        }
        private = {
            "player_id": p.pid,
            "reserved": [self.card_public(c) for c in p.reserved],
            "legal_actions": self.legal_actions(p),
        }
        return {"public": public, "private": private}

    # -- 动作执行 ----------------------------------------------------------
    def apply(self, p, act):
        events = []
        if not isinstance(act, dict):
            raise Illegal("动作必须是对象")
        kind = act.get("action")
        if kind == "take":
            events += self._do_take(p, act)
        elif kind == "reserve":
            events += self._do_reserve(p, act)
        elif kind == "buy":
            events += self._do_buy(p, act)
        elif kind == "pass":
            events.append(f"{p.name} 选择跳过")
        else:
            raise Illegal(f"未知动作 {kind!r}")
        events += self._check_nobles(p)
        return events

    def _do_take(self, p, act):
        gems = act.get("gems")
        if not isinstance(gems, list) or not gems:
            raise Illegal("take 需要 gems 列表")
        for g in gems:
            if g not in COLORS:
                raise Illegal(f"未知颜色 {g!r}")
        distinct = len(set(gems))
        avail_now = [c for c in COLORS if self.tokens[c] > 0]

        # 先在副本上算出结果，全部校验通过再落盘：
        # 否则 return 写错时宝石已经拿走了，动作被判非法却退不回来
        hand = dict(p.tokens)
        pool = dict(self.tokens)

        if distinct == 1 and len(gems) == 1:
            if len(avail_now) != 1:
                raise Illegal("还有别的颜色可选时不能只拿 1 枚")
            c = gems[0]
            hand[c] += 1
            pool[c] -= 1
            text = f"{p.name} 拿 {CN[c]}×1（仅剩此色）"
        elif distinct == 1:
            if len(gems) != 2:
                raise Illegal("同色只能拿 2 枚")
            c = gems[0]
            if self.tokens[c] < 4:
                raise Illegal(f"{CN[c]}宝石只剩 {self.tokens[c]} 枚，不足 4 枚不能拿 2 枚")
            hand[c] += 2
            pool[c] -= 2
            text = f"{p.name} 拿 {CN[c]}×2"
        else:
            if distinct != len(gems):
                raise Illegal("拿取的不同色宝石不能重复")
            want = min(self.max_take, len(avail_now))
            if len(gems) != want:
                raise Illegal(f"本局应拿 {want} 种不同颜色，收到 {len(gems)} 种")
            for g in gems:
                if self.tokens[g] <= 0:
                    raise Illegal(f"{CN[g]}宝石已空")
            for g in gems:
                hand[g] += 1
                pool[g] -= 1
            text = f"{p.name} 拿 " + "".join(CN[g] for g in gems)

        ret, ret_gold = self._plan_trim(hand, p.gold, act.get("return"))

        p.tokens = hand
        self.tokens = pool
        events = [text]
        if ret or ret_gold:
            self._apply_trim(p, ret, ret_gold)
            events.append(f"{p.name} 弃宝石，剩 {p.token_total} 枚")
        return events

    def _do_reserve(self, p, act):
        if len(p.reserved) >= 3:
            raise Illegal("预约牌已达 3 张上限")
        from_deck = act.get("from") == "deck"
        if from_deck:
            t = act.get("tier")
            if t not in (1, 2, 3):
                raise Illegal("tier 必须是 1/2/3")
            if not self.decks[t]:
                raise Illegal(f"{t} 级牌堆已空")
            card = self.decks[t][-1]
        else:
            cid = act.get("card")
            card = self._find_on_board(cid)
            if card is None:
                raise Illegal(f"场上没有这张卡：{cid}")

        will_gold = 1 if self.gold > 0 else 0
        ret, ret_gold = self._plan_trim(dict(p.tokens), p.gold + will_gold,
                                        act.get("return"))

        # 校验全过，落盘
        if from_deck:
            self.decks[t].pop()
        else:
            self._take_from_board(card["id"])
        p.reserved.append(card)
        events = [f"{p.name} 预约 {card['id']}"
                  f"（{CN[card['bonus']]}+{card['points']}分，费{fmt_cost(card['cost'])}）"]
        if will_gold:
            self.gold -= 1
            p.gold += 1
            events.append(f"{p.name} +1 金")
        else:
            events.append(f"{p.name} 想拿金但金已空")
        if ret or ret_gold:
            self._apply_trim(p, ret, ret_gold)
            events.append(f"{p.name} 弃宝石，剩 {p.token_total} 枚")
        return events

    def _find_on_board(self, card_id):
        """只查找，不改动棋盘。"""
        for t in (1, 2, 3):
            for c in self.board[t]:
                if c and c["id"] == card_id:
                    return c
        return None

    def _do_buy(self, p, act):
        cid = act.get("card")
        if not cid:
            raise Illegal("buy 需要 card 字段")
        # 先定位、先校验，确认无误后再动状态，避免非法动作把牌弄丢
        card = next((c for c in p.reserved if c["id"] == cid), None)
        from_reserve = card is not None
        if card is None:
            card = self._find_on_board(cid)
        if card is None:
            raise Illegal(f"买不到这张卡：{cid}")
        if not self.affordable(p, card):
            raise Illegal(f"宝石不足，买不起 {cid}")
        if from_reserve:
            p.reserved.remove(card)
        else:
            self._take_from_board(cid)

        pay, gold = self.payment(p, card)
        for c, v in pay.items():
            p.tokens[c] -= v
            self.tokens[c] += v
        if gold:
            p.gold -= gold
            self.gold += gold
        p.cards.append(card)
        p.bonuses[card["bonus"]] += 1

        cost_txt = fmt_gems(pay) + (f"+金{gold}" if gold else "")
        events = [f"{p.name} 买 {card['id']}"
                  f"（{CN[card['bonus']]}+1，{card['points']}分）付 {cost_txt} → {p.points}分"]
        return events

    # -- 宝石上限 ----------------------------------------------------------
    def _plan_trim(self, hand, gold, explicit):
        """算出要弃置哪些宝石，只计算不改状态；输入非法直接抛 Illegal。

        必须前置校验：以前是边改边算，负数会把宝石「倒加」进池子（实物只有 5 枚却能出现 6 枚），
        非数字则抛 ValueError，而那时宝石已经被拿走了。
        """
        total = sum(hand.values()) + gold
        if total <= self.token_limit:
            return {}, 0
        excess = total - self.token_limit
        ret, ret_gold = {}, 0

        if explicit is not None:
            if not isinstance(explicit, dict):
                raise Illegal("return 必须是 {颜色: 数量} 的对象")
            for c, v in explicit.items():
                if isinstance(v, bool) or not isinstance(v, int):
                    raise Illegal(f"return 里的 {c} 必须是整数")
                if v < 0:
                    raise Illegal(f"return 里的 {c} 不能是负数")
                if c == "gold":
                    avail = gold
                elif c in COLORS:
                    avail = hand.get(c, 0)
                else:
                    raise Illegal(f"return 里有未知颜色 {c!r}")
                if v > avail:
                    raise Illegal(f"return 里的 {c}={v} 超过手里数量 {avail}")
                if c == "gold":
                    ret_gold = v
                else:
                    ret[c] = v
                excess -= v
            if excess < 0:
                raise Illegal(f"return 弃多了：只需弃 {total - self.token_limit} 枚")
            if excess == 0:
                return ret, ret_gold

        # 没指定或没退够：从最多的颜色开始退，黄金最后退
        while excess > 0:
            pool = [(c, hand.get(c, 0) - ret.get(c, 0)) for c in COLORS]
            pool = [(c, n) for c, n in pool if n > 0]
            if pool:
                c, _ = max(pool, key=lambda kv: kv[1])
                ret[c] = ret.get(c, 0) + 1
            elif gold - ret_gold > 0:
                ret_gold += 1
            else:
                break
            excess -= 1
        return ret, ret_gold

    def _apply_trim(self, p, ret, ret_gold):
        for c, n in ret.items():
            p.tokens[c] -= n
            self.tokens[c] += n
        if ret_gold:
            p.gold -= ret_gold
            self.gold += ret_gold

    # -- 贵族 --------------------------------------------------------------
    def _check_nobles(self, p):
        for n in self.nobles:
            if n["taken_by"] is not None:
                continue
            if all(p.bonuses[c] >= v for c, v in n["requires"].items()):
                n["taken_by"] = p.pid
                p.nobles.append(n)
                return [f"★ {p.name} 获贵族 {n['id']}（{fmt_cost(n['requires'])}）→ {p.points}分"]
        return []

    # -- 棋盘快照 ----------------------------------------------------------
    def board_text(self):
        lines = []
        lines.append("宝石池 " + " ".join(f"{CN[c]}{self.tokens[c]}" for c in COLORS)
                     + f" 金{self.gold}")
        noble_txt = " | ".join(f"{x['id']} {fmt_cost(x['requires'])}"
                               for x in self.nobles if x["taken_by"] is None)
        if noble_txt:
            lines.append("贵族   " + noble_txt)
        for t in (3, 2, 1):
            parts = []
            for c in self.board[t]:
                if c:
                    parts.append(f"{c['id']}({CN[c['bonus']]}+1,{c['points']}分,{fmt_cost(c['cost'])})")
            lines.append(f"{t} 级   " + "  ".join(parts))
        for p in self.players:
            lines.append(f"{p.name} {p.points}分 宝石{fmt_gems(p.tokens)} 金{p.gold} "
                         f"加成{fmt_gems(p.bonuses)} 卡{len(p.cards)} 预约{len(p.reserved)}")
        return "\n".join(lines)

    # -- 主循环 ------------------------------------------------------------
    def run(self):
        log(f"璀璨宝石开局：{self.n} 人，目标 {self.target} 分，"
            f"每种宝石 {TOKENS_BY_PLAYERS.get(self.n, 7)} 枚，贵族 {len(self.nobles)} 张")
        if self.max_take != 3:
            log(f"本局变体：每回合最多拿 {self.max_take} 种不同颜色的宝石")
        log("玩家：" + "、".join(f"{p.name}" for p in self.players))

        idx = 0
        max_turns = int(self.cfg.get("max_turns", 400))
        while True:
            if self.turn_no >= max_turns:
                log(f"对局超过 {max_turns} 手仍未结束，强制结算", "warn")
                break
            p = self.players[idx]
            self.turn_no += 1
            view = self.build_view(p)
            send({"v": V, "type": "ask", "turn": self.turn_no,
                  "requests": [{"player": p.pid, "view": view,
                                "deadline_ms": self.deadline_ms}]})

            msg = recv()
            if msg.get("type") == "shutdown":
                sys.exit(0)
            actions = msg.get("actions") or {}
            res = actions.get(p.pid) or {}

            if not res.get("ok"):
                reason = res.get("error") or "unknown"
                log(f"{p.name} 本回合弃权（{reason}）", "warn")
            else:
                try:
                    for line in self.apply(p, res.get("action")):
                        log(line)
                except Illegal as exc:
                    p.illegal += 1
                    log(f"{p.name} 动作非法：{exc}，本回合作废", "warn")
                except Exception as exc:  # noqa: BLE001
                    p.illegal += 1
                    log(f"{p.name} 动作导致异常：{exc!r}，本回合作废", "warn")

            if not self.end_triggered and p.points >= self.target:
                self.end_triggered = True
                log(f"◆ {p.name} 达到 {p.points} 分，本轮结束后结算")

            if self.verbose:
                log(self.board_text())

            idx += 1
            if idx >= self.n:
                idx = 0
                self.round += 1
                if self.end_triggered:
                    break

        return self._result()

    def _result(self):
        order = sorted(self.players, key=lambda p: (-p.points, len(p.cards), p.seat))
        ranking = [p.pid for p in order]
        log("最终排名：" + " > ".join(
            f"{p.name}({p.points}分/{len(p.cards)}卡)" for p in order))
        return {
            "ranking": ranking,
            "scores": {p.pid: float(p.points) for p in self.players},
            "winner": ranking[0] if ranking else None,
            "reason": f"{self.round - 1} 轮结束，{self.turn_no} 手",
            "extra": {
                "rounds": self.round - 1,
                "turns": self.turn_no,
                "players": {
                    p.pid: {
                        "points": p.points,
                        "cards": len(p.cards),
                        "nobles": [n["id"] for n in p.nobles],
                        "bonuses": dict(p.bonuses),
                        "tokens": dict(p.tokens),
                        "gold": p.gold,
                        "illegal": p.illegal,
                    } for p in self.players
                },
            },
        }


def main():
    init = recv()
    if init.get("type") != "init":
        send({"v": V, "type": "error", "message": "期望 init"})
        return
    players = init.get("players") or []
    if len(players) < 2:
        send({"v": V, "type": "error", "message": "璀璨宝石至少需要 2 名玩家"})
        return
    if len(players) > 5:
        send({"v": V, "type": "error", "message": "最多支持 5 名玩家"})
        return

    send({"v": V, "type": "ready", "name": "璀璨宝石",
          "players": {"min": 2, "max": 5}})
    if recv().get("type") != "run":
        send({"v": V, "type": "error", "message": "期望 run"})
        return

    game = Splendor(players, int(init.get("seed", 0)), init.get("config") or {})
    try:
        result = game.run()
    except (BrokenPipeError, KeyboardInterrupt):
        return
    except Exception as exc:  # noqa: BLE001
        send({"v": V, "type": "error", "message": f"游戏异常：{exc!r}"})
        return
    send({"v": V, "type": "over", "result": result})


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
