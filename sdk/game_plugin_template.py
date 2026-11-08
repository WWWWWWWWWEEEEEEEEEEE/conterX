#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""插件式游戏模板 —— 不想单独起进程时用这个。

中转站会 import 这个文件，找到 create_game()，然后驱动 run(ctx)。
run 必须是一个生成器：yield 一次询问，就收回一次动作。
"""


class MyGame:
    name = "我的插件式游戏"

    def run(self, ctx):
        pids = ctx.player_ids
        if len(pids) < 2:
            raise ValueError("至少需要 2 名玩家")

        rounds = int(ctx.config.get("rounds", 5))
        score = {p: 0 for p in pids}

        ctx.log(f"开局：{len(pids)} 人，{rounds} 回合")

        for turn in range(1, rounds + 1):
            public = {
                "turn": turn,
                "rounds": rounds,
                "players": ctx.players,
                "scores": dict(score),
            }
            # 同时问所有人；只想问一个人就写 ctx.ask({pid: view})
            actions = yield ctx.ask(
                {p: {"public": public, "private": {"your_score": score[p]}} for p in pids},
                deadline_ms=2000,
            )

            for p in pids:
                res = (actions or {}).get(p) or {}
                if not res.get("ok"):
                    ctx.log(f"{ctx.name_of(p)} 本回合弃权（{res.get('error')}）", "warn")
                    continue
                act = res.get("action") or {}
                try:
                    gain = int(act.get("value", 0))
                except (TypeError, ValueError):
                    gain = 0
                score[p] += max(0, min(gain, 10))

            ctx.log("第 %d 回合结束：%s" % (
                turn, "、".join(f"{ctx.name_of(p)} {score[p]}" for p in pids)))

        ranking = sorted(pids, key=lambda p: -score[p])
        return {
            "ranking": ranking,
            "scores": {p: float(score[p]) for p in pids},
            "winner": ranking[0],
            "reason": f"{rounds} 回合结束",
        }


def create_game():
    return MyGame()
