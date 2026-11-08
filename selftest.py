#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""无界面自检：验证协议、两种游戏形态、两种玩家形态、违规处理、批量评测。

    python selftest.py

跑通说明内核正常，界面只是它的外壳。
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from core.batch import BatchConfig, BatchRunner
from core.engine import MatchConfig, MatchEngine
from core.games import make_game
from core.players import SeatConfig
from core.store import Store


def seats(paths):
    out = []
    colors = ["#d9534f", "#4a90d9", "#4caf50"]
    for i, (kind, target, extra) in enumerate(paths):
        out.append(SeatConfig(
            pid=f"p{i + 1}", name=f"玩家{i + 1}", kind=kind,
            target=os.path.join(BASE, target), args="", timeout_ms=2000,
            color=colors[i % 3], note="", **extra,
        ))
    return out


def show(report, title):
    print(f"\n=== {title} ===")
    print(f"游戏：{report.game_name}  回合：{report.turns}  用时：{report.duration_ms}ms")
    if report.error:
        print(f"错误：{report.error}")
    for row in report.ranking_with_status():
        flag = " [判负]" if row["disqualified"] else ""
        print(f"  #{row['rank']} {row['name']:<6} 得分 {row['score']:>6g} "
              f"违规 {row['violations']} 平均 {row['avg_ms']}ms{flag}")
    if report.result and report.result.reason:
        print(f"  → {report.result.reason}")


def scenario_auction():
    seat_list = seats([
        ("process", "examples/players/bid_random.py", {}),
        ("process", "examples/players/bid_greedy.py", {}),
        ("process", "examples/players/bid_shade.py", {}),
    ])
    game_path = os.path.join(BASE, "examples/games/sealed_bid.py")
    engine = MatchEngine(
        lambda: make_game("process", game_path, seed=7, config={"rounds": 6}),
        seat_list, MatchConfig(seed=7),
        log_dir=os.path.join(BASE, "data/logs"),
    )
    show(engine.run(), "场景一：进程式游戏 + 3 个进程式玩家")


def scenario_race():
    seat_list = seats([
        ("module", "examples/players/race21_solver.py", {}),
        ("module", "examples/players/race21_solver.py", {}),
        ("module", "examples/players/race21_solver.py", {}),
    ])
    game_path = os.path.join(BASE, "examples/games/race21_plugin.py")
    engine = MatchEngine(
        lambda: make_game("plugin", game_path, seed=3),
        seat_list, MatchConfig(seed=3),
        log_dir=os.path.join(BASE, "data/logs"),
    )
    show(engine.run(), "场景二：插件式游戏 + 3 个模块式玩家")


def scenario_violation():
    seat_list = seats([
        ("process", "examples/players/bid_greedy.py", {}),
        ("process", "examples/players/unstable.py", {}),
        ("process", "examples/players/bid_shade.py", {}),
    ])
    game_path = os.path.join(BASE, "examples/games/sealed_bid.py")
    engine = MatchEngine(
        lambda: make_game("process", game_path, seed=11, config={"rounds": 6}),
        seat_list, MatchConfig(seed=11, max_violations=2, default_timeout_ms=800),
        log_dir=os.path.join(BASE, "data/logs"),
    )
    show(engine.run(), "场景三：违规处理（超时/崩溃/非 JSON/空动作）")


def scenario_batch():
    seat_list = seats([
        ("process", "examples/players/bid_greedy.py", {}),
        ("process", "examples/players/bid_shade.py", {}),
        ("process", "examples/players/bid_random.py", {}),
    ])
    game_path = os.path.join(BASE, "examples/games/sealed_bid.py")
    store = Store(BASE)
    runner = BatchRunner(
        lambda: make_game("process", game_path, config={"rounds": 5}),
        seat_list, MatchConfig(),
        BatchConfig(rounds=6, seed_base=100),
        log_dir=store.log_dir, batch_dir=store.batch_dir,
    )
    report = runner.run()
    print("\n=== 场景四：批量对抗 6 局 ===")
    for row in report.rows:
        print(f"  #{row['rank']} {row['name']:<6} 胜 {row['wins']}/{row['matches']} "
              f"胜率 {row['win_rate']}% 均分 {row['avg_score']} "
              f"违规 {row['violations']} 均耗时 {row['avg_ms']}ms")
    print(f"  CSV：{report.csv_path}")
    if report.errors:
        print(f"  异常：{report.errors[:3]}")


if __name__ == "__main__":
    scenario_auction()
    scenario_race()
    scenario_violation()
    scenario_batch()
    print("\n自检完成。")
