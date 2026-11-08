#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BUGREPORT.md 的回归测试 —— 每条都复现原来的场景，再验证现在不再出错。

    python tests/test_engine_robustness.py

不依赖 GUI；用「脚本化游戏适配器 + 临时玩家脚本」精确控制时序。
"""

import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import protocol
from core.engine import MatchConfig, MatchEngine
from core.games import GameAdapter, GameFault, make_game
from core.players import SeatConfig
from core.protocol import Ask, GameResult, Request
from core.store import Store
from core.transport import (LinkClosed, LinkTimeout, ProcessLink, build_command)

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class ScriptedGame(GameAdapter):
    """按剧本发问的假游戏，用来精确控制引擎时序。"""

    name = "脚本游戏"

    def __init__(self, asks):
        self.asks = asks
        self.index = 0
        self.received = []
        self.turn_ms = 0.0
        self.stamps = []          # 每次 send_actions 的时刻，用来精确量单回合耗时

    def start(self, players, seed):
        return self.asks[0]

    def send_actions(self, turn, actions):
        self.stamps.append(time.perf_counter())
        if len(self.stamps) >= 2:
            self.turn_ms = (self.stamps[-1] - self.stamps[-2]) * 1000
        self.received.append((turn, actions))
        self.index += 1
        if self.index < len(self.asks):
            return self.asks[self.index]
        return GameResult(ranking=[p for p in ("p1", "p2", "p3")], scores={}, winner="p1")


def write_script(tmpdir, name, body):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))
    return path


def seat(pid, target, timeout_ms=2000, kind="process"):
    return SeatConfig(pid=pid, name=pid.upper(), kind=kind,
                      target=target, timeout_ms=timeout_ms)


def run_engine(game, seats, cfg=None, log_dir=""):
    engine = MatchEngine(lambda: game, seats, cfg or MatchConfig(), log_dir=log_dir)
    return engine.run()


# ---------------------------------------------------------------------------
# #3 玩家多吐一条 action → 整局动作错位
# ---------------------------------------------------------------------------
def test_turn_desync(tmpdir):
    print("\n[#3] 玩家多吐一条 action 不会被当成下一回合的答案")
    path = write_script(tmpdir, "double_action.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                t = msg["turn"]
                send({"v": 1, "type": "action", "turn": t, "action": {"n": t}})
                send({"v": 1, "type": "action", "turn": t, "action": {"n": t}})  # 多吐一条
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    asks = [Ask(turn=i, requests=[Request("p1", {"public": {}})]) for i in range(1, 6)]
    game = ScriptedGame(asks)
    report = run_engine(game, [seat("p1", path)], MatchConfig(max_violations=10))

    got = [(t, a.get("p1", {}).get("action")) for t, a in game.received]
    misaligned = [1 for t, act in got if isinstance(act, dict) and act.get("n") != t]
    check("没有出现「第 2 回合拿到第 1 回合答案」", not misaligned, got)
    check("回合错位被记为违规", any(s.violations > 0 for s in report.seats.values()),
          {k: v.violations for k, v in report.seats.items()})


# ---------------------------------------------------------------------------
# #2 同时制必须真并发
# ---------------------------------------------------------------------------
def test_concurrent_asks(tmpdir):
    print("\n[#2] 同时制是并发询问（一个回合的耗时 ≈ 单人耗时）")
    path = write_script(tmpdir, "slow400.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.4)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": {"ok": 1}})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    # 第 1 回合只问一个人（把子进程启动开销隔离掉），第 2 回合同时问三个人
    asks = [Ask(turn=1, requests=[Request("p1", {"public": {}})]),
            Ask(turn=2, requests=[Request(f"p{i}", {"public": {}}) for i in (1, 2, 3)])]
    game = ScriptedGame(asks)
    report = run_engine(game, [seat(f"p{i}", path) for i in (1, 2, 3)],
                        MatchConfig(default_timeout_ms=1500))
    cost = game.turn_ms
    # 串行至少 1200ms，并发应在 450ms 左右
    check(f"3 人同时思考 400ms，单回合 {cost:.0f}ms < 900ms", cost < 900, f"{cost:.0f}ms")
    check("并发下没有误判超时", all(s.violations == 0 for s in report.seats.values()),
          {k: v.violations for k, v in report.seats.items()})
    last = game.received[-1][1] if game.received else {}
    check("三个人都拿到了动作",
          all((last.get(f"p{i}") or {}).get("ok") for i in (1, 2, 3)), last)


# ---------------------------------------------------------------------------
# #6 停止能打断在途回合
# ---------------------------------------------------------------------------
def test_stop_interrupts(tmpdir):
    print("\n[#6] 点停止能立刻打断在途回合")
    path = write_script(tmpdir, "slow10s.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(10)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    reqs = [Request(f"p{i}", {"public": {}}) for i in (1, 2, 3)]
    game = ScriptedGame([Ask(turn=1, requests=reqs)])
    engine = MatchEngine(lambda: game, [seat(f"p{i}", path) for i in (1, 2, 3)],
                         MatchConfig(default_timeout_ms=60000))

    th = threading.Thread(target=engine.run, daemon=True)
    th.start()
    time.sleep(0.4)
    t0 = time.perf_counter()
    engine.stop()
    th.join(5)
    cost = time.perf_counter() - t0
    check(f"停止后 {cost:.2f}s 退出（原来是等满 deadline）", cost < 1.5, f"{cost:.2f}s")
    check("线程确实结束了", not th.is_alive())


# ---------------------------------------------------------------------------
# #4 NaN 不能被当成合法动作
# ---------------------------------------------------------------------------
def test_nan_action(tmpdir):
    print("\n[#4] NaN 动作被拦下，不会发给游戏")
    path = write_script(tmpdir, "nan_player.py", '''
        import json, math, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                sys.stdout.write('{"v":1,"type":"action","turn":%d,"action":{"bid":NaN}}\\n'
                                 % msg["turn"])
                sys.stdout.flush()
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})])])
    report = run_engine(game, [seat("p1", path)])
    act = game.received[0][1].get("p1") if game.received else {}
    check("游戏收到的是 ok=false，而不是 NaN", act.get("ok") is False, act)
    check("记了一次违规", report.seats["p1"].violations == 1,
          report.seats["p1"].violations)


# ---------------------------------------------------------------------------
# #5 游戏不递增 turn 时的死循环兜底
# ---------------------------------------------------------------------------
def test_stuck_turn():
    print("\n[#5] 游戏把 turn 写死成 1 也会被兜底中断")
    asks = [Ask(turn=1, requests=[Request("p1", {"public": {}})])] * 500
    game = ScriptedGame(asks)
    cfg = MatchConfig(max_asks=30, max_violations=99)
    t0 = time.perf_counter()
    report = run_engine(game, [SeatConfig(pid="p1", name="P1", kind="module",
                                          target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                        cfg)
    cost = time.perf_counter() - t0
    check("被判定为疑似死循环并中断", "死循环" in (report.error or ""), report.error)
    check("没有真的跑 500 次", len(game.received) <= 31, len(game.received))
    check("用时合理（不是无限循环）", cost < 10, f"{cost:.1f}s")


# ---------------------------------------------------------------------------
# #7 游戏侧协议错误应归类为「游戏异常」
# ---------------------------------------------------------------------------
def test_game_protocol_error(tmpdir):
    print("\n[#7] 游戏发了不合法的 over，归类为游戏协议错误")
    path = write_script(tmpdir, "bad_over_game.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        init = recv()
        send({"v": 1, "type": "ready", "name": "坏游戏"})
        recv()
        send({"v": 1, "type": "ask", "turn": 1,
              "requests": [{"player": init["players"][0]["id"], "view": {}}]})
        recv()
        send({"v": 1, "type": "over", "result": {"ranking": ["p1"], "scores": [1, 2]}})
    ''')
    report = run_engine(make_game("process", path), [seat("p1", path)],
                        MatchConfig())
    err = report.error or ""
    check("错误信息指明是游戏协议问题", "游戏协议错误" in err, err)
    check("没有甩锅成「引擎异常」", "引擎异常" not in err, err)


# ---------------------------------------------------------------------------
# #9 重复 pid 不再漏关子进程
# ---------------------------------------------------------------------------
def test_duplicate_pid(tmpdir):
    print("\n[#9] 重复 pid 会被改名，两个席位都保留")
    path = write_script(tmpdir, "quick.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    seats = [seat("p1", path), seat("p1", path)]
    engine = MatchEngine(lambda: ScriptedGame(
        [Ask(turn=1, requests=[Request("p1", {}), Request("p1-2", {})])]), seats, MatchConfig())
    report = engine.run()
    pids = [s.pid for s in engine.seats]
    check("两个席位 pid 不同", len(set(pids)) == 2, pids)
    check("统计里两条都在", len(report.seats) == 2, list(report.seats))
    check("有改名告警", any("重复" in w for w in engine.warnings), engine.warnings)


# ---------------------------------------------------------------------------
# #11 坏配置不许崩
# ---------------------------------------------------------------------------
def test_corrupt_config(tmpdir):
    print("\n[#11] players.json / settings.json 写坏了也能启动")
    data_dir = os.path.join(tmpdir, "cfg", "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "players.json"), "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")
    with open(os.path.join(data_dir, "settings.json"), "w", encoding="utf-8") as f:
        f.write('{"seed": "abc", "max_violations": null, "game": "oops"}')
    ok = True
    try:
        store = Store(os.path.join(tmpdir, "cfg"))
        seats = store.load_players()
        settings = store.load_settings()
    except Exception as exc:  # noqa: BLE001
        ok = False
        seats, settings = [], {}
        print("       ", exc)
    check("加载不抛异常", ok)
    check("席位有兜底", len(seats) >= 1, len(seats))
    check("seed 非法时回落成整数", isinstance(settings.get("seed"), int), settings.get("seed"))
    check("game 字段非法时回落成字典", isinstance(settings.get("game"), dict),
          type(settings.get("game")))


# ---------------------------------------------------------------------------
# #12 失败动作上报真实耗时
# ---------------------------------------------------------------------------
def test_failure_elapsed(tmpdir):
    print("\n[#12] 崩溃动作的 elapsed_ms 是真实耗时，不是 deadline")
    path = write_script(tmpdir, "die_fast.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.05)
                sys.exit(3)
    ''')
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})])])
    run_engine(game, [seat("p1", path, timeout_ms=5000)], MatchConfig(default_timeout_ms=5000))
    act = game.received[0][1].get("p1", {}) if game.received else {}
    ms = act.get("elapsed_ms", -1)
    check("上报耗时接近真实的 50ms", 0 <= ms < 1500, ms)


# ---------------------------------------------------------------------------
# #13 排名去重
# ---------------------------------------------------------------------------
def test_ranking_dedupe():
    print("\n[#13] 排名里重复的 pid 会被去掉")
    r = protocol.parse_result({"ranking": ["p1", "p1", "p2"]}, ["p1", "p2"])
    check("排名不重复", r.ranking == ["p1", "p2"], r.ranking)


# ---------------------------------------------------------------------------
# #15 插件式空 requests
# ---------------------------------------------------------------------------
def test_plugin_empty_ask(tmpdir):
    print("\n[#15] 插件 yield 空 requests 会被拒绝（与进程式一致）")
    path = write_script(tmpdir, "empty_ask_game.py", '''
        class G:
            name = "空请求"
            def run(self, ctx):
                yield ctx.ask([])
        def create_game():
            return G()
    ''')
    report = run_engine(lambda: make_game("plugin", path),
                        [SeatConfig(pid="p1", name="P1", kind="module",
                                    target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                        MatchConfig())
    check("报了错而不是静默跑完", bool(report.error), report.error)


# ---------------------------------------------------------------------------
# #10 版本号宽松校验
# ---------------------------------------------------------------------------
def test_version_field():
    print("\n[#10] v 字段兼容 \"1\" / 1.0 / v:2（conterx/2），拒绝 true 与更高版本")
    # v:2 自 AGENT-PROTOCOL.md 起为受支持的智能体扩展版本；v:3 仍应拒绝。
    cases = [("1", True), (1.0, True), (1, True), (2, True), ("2", True),
             (True, False), (3, False), ("abc", False)]
    for val, expect_ok in cases:
        try:
            protocol.check_envelope({"v": val, "type": "ask"})
            got = True
        except protocol.ProtocolError:
            got = False
        check(f"v={val!r} {'通过' if expect_ok else '被拒'}", got == expect_ok)


# ---------------------------------------------------------------------------
# #16 并发下日志序号不重复
# ---------------------------------------------------------------------------
def test_log_seq_unique(tmpdir):
    print("\n[#16] 并发询问时日志序号不重复、不乱序")
    path = write_script(tmpdir, "echo.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                for _ in range(3):
                    send({"v": 1, "type": "log", "text": "思考中"})
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    reqs = [Request(f"p{i}", {"public": {}}) for i in (1, 2, 3)]
    game = ScriptedGame([Ask(turn=1, requests=reqs), Ask(turn=2, requests=reqs)])
    logs = []
    engine = MatchEngine(lambda: game, [seat(f"p{i}", path) for i in (1, 2, 3)],
                         MatchConfig(), hooks={"on_log": logs.append})
    engine.run()
    seqs = [e["seq"] for e in logs]
    check("seq 唯一", len(seqs) == len(set(seqs)), f"{len(seqs)} 条 / {len(set(seqs))} 个不同")
    check("seq 严格递增", seqs == sorted(seqs), seqs[:10])


# ---------------------------------------------------------------------------
# #18 批量 CSV 不会互相覆盖
# ---------------------------------------------------------------------------
def test_batch_csv_unique(tmpdir):
    print("\n[#18] 同一秒内跑两次批量，CSV 不互相覆盖")
    from core.batch import BatchConfig, BatchRunner
    path = write_script(tmpdir, "fast.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    out = os.path.join(tmpdir, "batchout")
    paths = []
    for _ in range(2):
        runner = BatchRunner(lambda: ScriptedGame([Ask(turn=1, requests=[Request("p1", {})])]),
                             [seat("p1", path)], MatchConfig(),
                             BatchConfig(rounds=1, seed_base=1), batch_dir=out)
        paths.append(runner.run().csv_path)
    check("两次批量产出两个文件", paths[0] != paths[1], paths)
    check("两个文件都存在", all(p and os.path.exists(p) for p in paths), paths)


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P1-1 缺运行时时不能整局作废
# ---------------------------------------------------------------------------
def test_missing_runtime(tmpdir):
    print("\n[P1-1] 程序起不来时记违规，而不是「引擎异常」")
    bad = os.path.join(tmpdir, "bad.exe")
    os.makedirs(bad, exist_ok=True)          # 目录冒充可执行文件 → Popen 抛 OSError
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})]),
                         Ask(turn=2, requests=[Request("p1", {"public": {}})])])
    report = run_engine(game, [seat("p1", bad)], MatchConfig(max_violations=5))
    err = report.error or ""
    check("整局没有被判成「引擎异常」", "引擎异常" not in err, err)
    check("该席位记了违规", report.seats["p1"].violations >= 1,
          report.seats["p1"].violations)
    check("状态说明了启动失败", "无法启动" in report.seats["p1"].status,
          report.seats["p1"].status)


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P1-2 游戏 view 里有 NaN
# ---------------------------------------------------------------------------
def test_nan_in_view(tmpdir):
    print("\n[P1-2] 游戏在 view 里塞 NaN，报「游戏异常」而不是引擎裸崩")
    path = write_script(tmpdir, "nan_view_game.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        init = recv()
        send({"v": 1, "type": "ready", "name": "坏视野"})
        recv()
        pid = init["players"][0]["id"]
        # json.dumps 默认会输出 NaN 字面量
        send({"v": 1, "type": "ask", "turn": 1,
              "requests": [{"player": pid, "view": {"avg": float("nan")}}]})
        recv()
        send({"v": 1, "type": "over", "result": {"ranking": [pid]}})
    ''')
    report = run_engine(make_game("process", path), [seat("p1", path)], MatchConfig())
    err = report.error or ""
    check("错误信息指明是 view 无法序列化", "view 无法序列化" in err, err)
    check("没有甩锅成「引擎异常」", "引擎异常" not in err, err)


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P1-3 启动握手也能被停止打断
# ---------------------------------------------------------------------------
def test_stop_during_handshake(tmpdir):
    print("\n[P1-3] 玩家握手很慢时，点停止不用等满 15s")
    path = write_script(tmpdir, "slow_ready.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv()
        time.sleep(10)          # 迟迟不 ready
        send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") in ("over", "shutdown"):
                break
    ''')
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})])])
    engine = MatchEngine(lambda: game, [seat("p1", path)], MatchConfig())
    th = threading.Thread(target=engine.run, daemon=True)
    th.start()
    time.sleep(1.0)
    t0 = time.perf_counter()
    engine.stop()
    th.join(6)
    cost = time.perf_counter() - t0
    check(f"停止后 {cost:.2f}s 退出（原来要等满 15s 握手超时）", cost < 2.5, f"{cost:.2f}s")
    check("线程结束了", not th.is_alive())


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P2-1 收尾并发关闭
# ---------------------------------------------------------------------------
def test_close_is_parallel(tmpdir):
    print("\n[P2-1] 收尾时并发关闭子进程（不再每个等 0.6s）")
    path = write_script(tmpdir, "slow2s.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(2)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    reqs = [Request(f"p{i}", {"public": {}}) for i in (1, 2, 3)]
    game = ScriptedGame([Ask(turn=1, requests=reqs)])
    engine = MatchEngine(lambda: game, [seat(f"p{i}", path) for i in (1, 2, 3)],
                         MatchConfig(default_timeout_ms=30000))
    th = threading.Thread(target=engine.run, daemon=True)
    th.start()
    time.sleep(1.4)          # 等到三个人都进了 sleep(2)，收尾才有意义
    t0 = time.perf_counter()
    engine.stop()
    th.join(8)
    cost = time.perf_counter() - t0
    # 串行关 3 个进程 ≈ 1.9s，并发 ≈ 0.7s
    check(f"停止+收尾共 {cost:.2f}s（串行要 1.9s 以上）", cost < 1.3, f"{cost:.2f}s")


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P2-2 幽灵 pid
# ---------------------------------------------------------------------------
def test_ghost_pid():
    print("\n[P2-2] 排名里不存在的 pid 会被过滤")
    warns = []
    r = protocol.parse_result({"ranking": ["ghost", "p1", "p2"], "scores": {"p1": 3}},
                              ["p1", "p2"], on_warn=warns.append)
    check("幽灵 pid 被剔除", r.ranking == ["p1", "p2"], r.ranking)
    check("给出了告警", any("ghost" in w for w in warns), warns)


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P2-3 分数 NaN
# ---------------------------------------------------------------------------
def test_nan_score(tmpdir):
    print("\n[P2-3] 分数带 NaN 时按 0 处理，日志的 report 行不再丢")
    path = write_script(tmpdir, "nan_score_game.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        init = recv()
        send({"v": 1, "type": "ready", "name": "NaN 分数"})
        recv()
        pid = init["players"][0]["id"]
        send({"v": 1, "type": "ask", "turn": 1,
              "requests": [{"player": pid, "view": {"public": {}}}]})
        recv()
        send({"v": 1, "type": "over",
              "result": {"ranking": [pid], "scores": {pid: float("nan")}}})
    ''')
    logs = os.path.join(tmpdir, "nanlogs")
    report = run_engine(make_game("process", path), [seat("p1", path)],
                        MatchConfig(), log_dir=logs)
    check("分数被压成 0", report.result is not None
          and report.result.scores.get("p1") == 0.0,
          report.result.scores if report.result else None)
    lines = []
    if report.log_path and os.path.exists(report.log_path):
        with open(report.log_path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
    check("日志最后一行是 report", bool(lines) and '"report"' in lines[-1],
          lines[-1][:60] if lines else "（空）")


# ---------------------------------------------------------------------------
# BUGREPORT-2 · P3-2 drain 不能清掉崩溃标记
# ---------------------------------------------------------------------------
def test_drain_keeps_terminal(tmpdir):
    print("\n[P3-2] drain 之后仍能正确报「崩溃」而不是「超时」")
    path = write_script(tmpdir, "exit_now.py", '''
        import sys
        sys.exit(0)
    ''')
    link = ProcessLink(build_command(path), name="t")
    link.start()
    time.sleep(0.6)                    # 等 reader 线程读到 eof
    link.drain()
    try:
        link.recv(0.5)
        got = "没抛异常"
    except LinkClosed:
        got = "closed"
    except LinkTimeout:
        got = "timeout"
    link.close()
    check("drain 之后报的是 closed（不是 timeout）", got == "closed", got)


# ---------------------------------------------------------------------------
# BUGREPORT-3 · P1 负数 deadline 会让全员被判负
# ---------------------------------------------------------------------------
def test_negative_deadline(tmpdir):
    print("\n[BR3-P1] deadline_ms 为负数时明确报错，而不是全员超时判负")
    path = write_script(tmpdir, "neg_deadline_game.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        init = recv()
        send({"v": 1, "type": "ready", "name": "负数超时"})
        recv()
        pid = init["players"][0]["id"]
        send({"v": 1, "type": "ask", "turn": 1,
              "requests": [{"player": pid, "view": {}, "deadline_ms": -500}]})
        recv()
        send({"v": 1, "type": "over", "result": {"ranking": [pid]}})
    ''')
    quick = write_script(tmpdir, "quick200.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.2)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    report = run_engine(make_game("process", path), [seat("p1", quick)],
                        MatchConfig(default_timeout_ms=2000))
    err = report.error or ""
    check("错误信息点明 deadline 不能为负", "deadline_ms 不能是负数" in err, err)
    check("归类为游戏协议错误", "游戏协议错误" in err, err)
    check("没有任何玩家被判负",
          all(not st.disqualified and st.violations == 0 for st in report.seats.values()),
          {k: (v.violations, v.disqualified) for k, v in report.seats.items()})


def test_zero_deadline(tmpdir):
    print("\n[BR3-P1] deadline_ms=0 表示用默认值（不是 50ms）")
    path = write_script(tmpdir, "zero_deadline_game.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        init = recv()
        send({"v": 1, "type": "ready", "name": "零超时"})
        recv()
        pid = init["players"][0]["id"]
        send({"v": 1, "type": "ask", "turn": 1,
              "requests": [{"player": pid, "view": {}, "deadline_ms": 0}]})
        recv()
        send({"v": 1, "type": "over", "result": {"ranking": [pid]}})
    ''')
    quick = write_script(tmpdir, "quick200b.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.2)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    report = run_engine(make_game("process", path), [seat("p1", quick)],
                        MatchConfig(default_timeout_ms=2000))
    check("没有误判超时", report.seats["p1"].violations == 0,
          report.seats["p1"].violations)


# ---------------------------------------------------------------------------
# BUGREPORT-3 · P2 插件返回 GameResult 对象也要走校验
# ---------------------------------------------------------------------------
def test_plugin_gameresult_normalized(tmpdir):
    print("\n[BR3-P2] 插件返回 GameResult 对象时同样过滤幽灵 pid / NaN")
    path = write_script(tmpdir, "gameresult_game.py", '''
        from core.protocol import GameResult
        class G:
            name = "返回对象"
            def run(self, ctx):
                yield ctx.ask({"p1": {"public": {}}})
                return GameResult(ranking=["ghost", "p1"],
                                  scores={"p1": float("nan")})
        def create_game():
            return G()
    ''')
    report = run_engine(make_game("plugin", path),
                        [SeatConfig(pid="p1", name="P1", kind="module",
                                    target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                        MatchConfig())
    r = report.result
    check("幽灵 pid 被过滤", r is not None and r.ranking == ["p1"],
          r.ranking if r else None)
    check("NaN 分数归零", r is not None and r.scores.get("p1") == 0.0,
          r.scores if r else None)


# ---------------------------------------------------------------------------
# BUGREPORT-3 · P3 插件手写报文出错不能算「引擎异常」
# ---------------------------------------------------------------------------
def test_plugin_bad_ask(tmpdir):
    print("\n[BR3-P3] 插件手写 ask 报文不合法 → 游戏协议错误")
    path = write_script(tmpdir, "bad_ask_game.py", '''
        class G:
            name = "坏 ask"
            def run(self, ctx):
                yield {"requests": [{"player": "p1", "view": "不是对象"}]}
        def create_game():
            return G()
    ''')
    report = run_engine(make_game("plugin", path),
                        [SeatConfig(pid="p1", name="P1", kind="module",
                                    target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                        MatchConfig())
    err = report.error or ""
    check("归类为游戏协议错误", "游戏协议错误" in err, err)
    check("没有甩锅成引擎异常", "引擎异常" not in err, err)


# ---------------------------------------------------------------------------
# BUGREPORT-3 · P3 决策耗时不该包含进程启动
# ---------------------------------------------------------------------------
def test_elapsed_excludes_setup(tmpdir):
    print("\n[BR3-P3] 非常驻模式的「决策耗时」不再包含进程启动")
    path = write_script(tmpdir, "sleep200.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.2)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    asks = [Ask(turn=i, requests=[Request("p1", {"public": {}})]) for i in (1, 2, 3)]
    res = {}
    for persistent in (True, False):
        game = ScriptedGame(asks)
        report = run_engine(game, [seat("p1", path)],
                            MatchConfig(persistent_players=persistent,
                                        default_timeout_ms=5000))
        res[persistent] = report.seats["p1"].avg_ms
    check(f"常驻 {res[True]:.0f}ms / 非常驻 {res[False]:.0f}ms 差距 < 120ms",
          abs(res[True] - res[False]) < 120, res)
    check("非常驻也没有把启动算进去（应接近 200ms）", res[False] < 350, res[False])


# ---------------------------------------------------------------------------
# BUGREPORT-3 · 尾巴：日志保留 / 超长截断
# ---------------------------------------------------------------------------
def test_log_retention(tmpdir):
    print("\n[BR3-尾巴] 日志只保留最近 N 份")
    log_dir = os.path.join(tmpdir, "keep_logs")
    os.makedirs(log_dir, exist_ok=True)
    now = time.time()
    for i in range(6):
        p = os.path.join(log_dir, f"old{i}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.utime(p, (now - 100 + i, now - 100 + i))
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})])])
    engine = MatchEngine(lambda: game, [SeatConfig(pid="p1", name="P1", kind="module",
                                                   target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                         MatchConfig(log_keep=3), log_dir=log_dir)
    engine.run()
    left = [f for f in os.listdir(log_dir) if f.endswith(".jsonl")]
    check(f"清理后只剩 {len(left)} 份（上限 3）", len(left) == 3, left)


def test_log_truncation(tmpdir):
    print("\n[BR3-尾巴] 超大动作在日志里会被截断")
    log_dir = os.path.join(tmpdir, "big_logs")
    os.makedirs(log_dir, exist_ok=True)
    engine = MatchEngine(lambda: ScriptedGame([Ask(turn=1, requests=[Request("p1", {})])]),
                         [SeatConfig(pid="p1", name="P1", kind="module",
                                     target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                         MatchConfig(), log_dir=log_dir)
    path = os.path.join(log_dir, "big.jsonl")
    engine._log_fp = open(path, "w", encoding="utf-8")
    engine._emit("action", "大动作", data={"blob": "x" * 200000})
    engine._log_fp.close()
    with open(path, encoding="utf-8") as f:
        line = f.readline()
    check(f"单行 {len(line)} 字节，压到 64KB 以内", len(line) < 70000, len(line))
    check("标记了截断", "truncated" in line)


# ---------------------------------------------------------------------------
# 第四轮 · 界面的「超时」必须真的生效
# ---------------------------------------------------------------------------
def test_default_timeout_used(tmpdir):
    print("\n[R4-1] 游戏不发 deadline_ms 时，用界面的「超时」而不是席位超时")
    path = write_script(tmpdir, "sleep800.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.8)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    # 席位给 5000ms，界面默认只给 300ms：以前席位值直接覆盖默认值，这局不会超时
    game = ScriptedGame([Ask(turn=1, requests=[Request("p1", {"public": {}})])])
    report = run_engine(game, [seat("p1", path, timeout_ms=5000)],
                        MatchConfig(default_timeout_ms=300))
    check("按界面的 300ms 判了超时", report.seats["p1"].violations == 1,
          report.seats["p1"].violations)
    act = game.received[0][1].get("p1", {}) if game.received else {}
    check("游戏收到的是 timeout", act.get("error") == "timeout", act)


def test_seat_timeout_is_cap(tmpdir):
    print("\n[R4-2] 席位超时是上限：能把游戏给的时限压短")
    path = write_script(tmpdir, "sleep800b.py", '''
        import json, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                time.sleep(0.8)
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") in ("over", "shutdown"):
                break
    ''')
    game = ScriptedGame([Ask(turn=1, requests=[
        Request("p1", {"public": {}}, deadline_ms=3000)])])
    report = run_engine(game, [seat("p1", path, timeout_ms=400)],
                        MatchConfig(default_timeout_ms=5000))
    check("被席位 400ms 压住了", report.seats["p1"].violations == 1,
          report.seats["p1"].violations)


# ---------------------------------------------------------------------------
# 第四轮 · 非常驻模式也要能收到 over
# ---------------------------------------------------------------------------
def test_over_delivered_non_persistent(tmpdir):
    print("\n[R4-3] 非常驻模式下玩家仍能收到 over（协议里承诺过）")
    path = write_script(tmpdir, "writes_over.py", '''
        import json, sys
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def recv():
            line = sys.stdin.readline()
            if not line: sys.exit(0)
            return json.loads(line)
        recv(); send({"v": 1, "type": "ready"})
        while True:
            msg = recv()
            if msg.get("type") == "turn":
                send({"v": 1, "type": "action", "turn": msg["turn"], "action": 1})
            elif msg.get("type") == "over":
                with open("over_seen.txt", "w", encoding="utf-8") as f:
                    f.write(json.dumps(msg.get("you", {})))
                break
            elif msg.get("type") == "shutdown":
                break
    ''')
    asks = [Ask(turn=i, requests=[Request("p1", {"public": {}})]) for i in (1, 2)]
    run_engine(ScriptedGame(asks), [seat("p1", path)],
               MatchConfig(persistent_players=False))
    marker = os.path.join(tmpdir, "over_seen.txt")
    check("玩家进程收到了 over", os.path.exists(marker))
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            you = json.loads(f.read() or "{}")
        check("over 里带了名次/成绩", "rank" in you and "score" in you, you)


# ---------------------------------------------------------------------------
# 第四轮 · 插件路径也要校验 view 是对象
# ---------------------------------------------------------------------------
def test_plugin_view_must_be_object(tmpdir):
    print("\n[R4-4] 插件 ctx.ask 传非对象 view → 游戏协议错误")
    path = write_script(tmpdir, "list_view_game.py", '''
        class G:
            name = "列表视野"
            def run(self, ctx):
                yield ctx.ask({"p1": ["不是", "对象"]})
        def create_game():
            return G()
    ''')
    report = run_engine(make_game("plugin", path),
                        [SeatConfig(pid="p1", name="P1", kind="module",
                                    target=os.path.join(BASE, "examples/players/race21_solver.py"))],
                        MatchConfig())
    err = report.error or ""
    check("归类为游戏协议错误", "游戏协议错误" in err, err)
    check("指出 view 必须是对象", "view 必须是对象" in err, err)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tmpdir = tempfile.mkdtemp(prefix="conterx_test_")
    try:
        test_turn_desync(tmpdir)
        test_concurrent_asks(tmpdir)
        test_stop_interrupts(tmpdir)
        test_nan_action(tmpdir)
        test_stuck_turn()
        test_game_protocol_error(tmpdir)
        test_duplicate_pid(tmpdir)
        test_corrupt_config(tmpdir)
        test_failure_elapsed(tmpdir)
        test_ranking_dedupe()
        test_plugin_empty_ask(tmpdir)
        test_version_field()
        test_log_seq_unique(tmpdir)
        test_batch_csv_unique(tmpdir)
        test_missing_runtime(tmpdir)
        test_nan_in_view(tmpdir)
        test_stop_during_handshake(tmpdir)
        test_close_is_parallel(tmpdir)
        test_ghost_pid()
        test_nan_score(tmpdir)
        test_drain_keeps_terminal(tmpdir)
        test_negative_deadline(tmpdir)
        test_zero_deadline(tmpdir)
        test_plugin_gameresult_normalized(tmpdir)
        test_plugin_bad_ask(tmpdir)
        test_elapsed_excludes_setup(tmpdir)
        test_log_retention(tmpdir)
        test_log_truncation(tmpdir)
        test_default_timeout_used(tmpdir)
        test_seat_timeout_is_cap(tmpdir)
        test_over_delivered_non_persistent(tmpdir)
        test_plugin_view_must_be_object(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全部通过")
