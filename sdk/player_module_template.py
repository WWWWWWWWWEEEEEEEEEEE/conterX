#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模块式玩家模板 —— 最短的接入方式。

实现 decide(view, turn, ctx) 就够了。适合快速试算法、写基线对手。
注意：它跑在中转站进程内，超时只能放弃等待、无法强制杀掉，
所以重搜索/死循环风险高的算法请用进程式模板。
"""


def setup(ctx):
    """开局调用一次（可选）。"""
    ctx.log("模块式玩家已就位")


def decide(view, turn, ctx):
    """每回合调用一次（必须）。返回动作对象。"""
    public = view.get("public") or {}
    private = view.get("private") or {}

    # ↓↓↓ 换成你的算法 ↓↓↓
    # 模板游戏要的是 {"value": 0~10}，落后就多拿一点
    my_score = int(private.get("score", 0))
    value = 4 if my_score < 10 else 1
    # ↑↑↑ 换成你的算法 ↑↑↑

    return {"value": value}


def teardown(result, you):
    """局末调用一次（可选）。result 是最终排名，you 是你的成绩。"""
    pass
