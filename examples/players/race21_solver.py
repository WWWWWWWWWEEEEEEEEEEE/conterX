#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例玩家 D：抢 21 的最优解（模块式玩家模板）

模块式玩家不需要自己收发 JSON，只要实现三个可选函数：
    setup(ctx)                    可选，开局调用一次
    decide(view, turn, ctx)       必须，返回动作
    teardown(result, you)         可选，局末调用

优点：写起来最短，能直接 print 调试（输出走中转站日志）。
缺点：跑在中转站进程内，只能 Python，超时只能"放弃等待"而不能真正杀掉。
"""


def setup(ctx):
    ctx.log("最优解玩家已就位")


def decide(view, turn, ctx):
    public = view.get("public") or {}
    total = int(public.get("total", 0))
    target = int(public.get("target", 21))

    # 能一步到 21 就直接赢
    if total >= target - 3:
        return {"add": max(1, target - total)}

    # 把对手留在 total % 4 == 1 的必败位
    add = (1 - total) % 4
    if add == 0:
        add = 1
    return {"add": max(1, min(3, add))}


def teardown(result, you):
    pass
