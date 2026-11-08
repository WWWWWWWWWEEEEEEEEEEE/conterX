#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抢 21（插件式游戏示例）

规则
----
- 累计数从 0 开始，玩家轮流报 1~3，累加。
- 谁把累计数推到 ≥ 21 谁立刻获胜。
- 顺序制：每次只问一位玩家（演示 ask 只带一个 request 的用法）。

插件式游戏 = 一个 run(ctx) 生成器：
    actions = yield ctx.ask({玩家id: 视野})
    ...
    return {"ranking": [...], "scores": {...}}
中转站负责驱动它，和进程式游戏共用同一套引擎。
"""


class Race21:
    name = "抢21"
    target = 21

    def run(self, ctx):
        pids = ctx.player_ids
        if len(pids) < 2:
            raise ValueError("至少需要 2 名玩家")

        total = 0
        idx = 0
        history = []
        guard = 0

        ctx.log(f"抢 21 开始：{len(pids)} 人轮流报数，先到 21 者胜")

        while total < self.target:
            guard += 1
            if guard > 500:
                ctx.log("回合数异常，强制结束", "warn")
                break

            pid = pids[idx % len(pids)]
            view = {
                "public": {
                    "total": total,
                    "target": self.target,
                    "players": ctx.players,
                    "history": history[-6:],
                    "your_turn": pid,
                },
                "private": {},
            }
            actions = yield ctx.ask({pid: view}, deadline_ms=1500)

            res = (actions or {}).get(pid) or {}
            if not res.get("ok"):
                add = 1
                ctx.log(f"{ctx.name_of(pid)} 未给出有效动作（{res.get('error')}），按 +1 处理", "warn")
            else:
                act = res.get("action")
                raw = act.get("add") if isinstance(act, dict) else act
                try:
                    add = int(raw)
                except (TypeError, ValueError):
                    add = 1
                if add < 1 or add > 3:
                    ctx.log(f"{ctx.name_of(pid)} 报了非法数字 {raw}，按 +1 处理", "warn")
                    add = 1

            total += add
            history.append({"player": pid, "name": ctx.name_of(pid), "add": add, "total": total})
            ctx.log(f"{ctx.name_of(pid)} +{add} → {total}")

            if total >= self.target:
                others = [p for p in pids if p != pid]
                ranking = [pid] + others
                scores = {p: (float(self.target) if p == pid else 0.0) for p in pids}
                return {
                    "ranking": ranking,
                    "scores": scores,
                    "winner": pid,
                    "reason": f"{ctx.name_of(pid)} 率先到达 {total}",
                }

            idx += 1

        # 保险分支
        return {"ranking": list(pids), "scores": {p: 0.0 for p in pids},
                "reason": "对局异常结束"}


def create_game():
    return Race21()
