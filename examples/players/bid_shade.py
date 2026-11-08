#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例玩家 C：看公开成交价压价（"读盘"型）

思路：只利用 public 里的历史成交价，估计别人的心理价位，再决定要不要跟。
- 只在自己估值明显高于市场价时才出手；
- 出价 = 市场价 × 系数，并留出利润空间；
- 预算吃紧时自动收手。
这是"利用中转站公开信息做推理"的最小示范。
"""

import json
import statistics
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def main():
    init = recv()
    cfg = init.get("config") or {}
    factor = float(cfg.get("factor", 1.08))     # 出价相对市场价的溢价
    edge = int(cfg.get("edge", 6))              # 至少要赚多少才出手
    send({"v": 1, "type": "ready"})

    while True:
        msg = recv()
        mtype = msg.get("type")
        if mtype == "turn":
            view = msg.get("view") or {}
            public = view.get("public") or {}
            private = view.get("private") or {}
            budget = int(private.get("budget", 0))
            valuation = int(private.get("valuation", 0))

            prices = [h.get("price", 0) for h in (public.get("history") or []) if h.get("sold")]
            market = statistics.median(prices) if prices else valuation * 0.6

            bid = 0
            if valuation - market >= edge and budget > 0:
                bid = int(round(min(budget, market * factor, valuation - 1)))
                bid = max(0, bid)
            send({"v": 1, "type": "action", "turn": msg.get("turn"), "action": {"bid": bid}})
        elif mtype == "over":
            break
        elif mtype == "shutdown":
            break


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
