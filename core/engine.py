"""对局引擎：驱动「游戏 ↔ 玩家」的回合循环。

职责（也是本软件的定位）：
1. 把游戏产出的请求按玩家裁剪后下发（私有视野天然隔离：p1 只能拿到自己的 view）；
2. 收集动作并做超时 / 崩溃 / 协议校验，按策略记违规、判负；
3. 把规范化后的动作回传给游戏；
4. 全程写回合日志（内存 + data/logs/*.jsonl）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from concurrent.futures import ThreadPoolExecutor

from . import protocol
from .games import GameAdapter, GameFault
from .players import PlayerAborted, PlayerFault, SeatConfig, make_player
from .protocol import Ask, GameResult

MAX_TURNS = 20000
MAX_LOG_LINE = 64 * 1024      # 单条日志上限：动作里塞了 1MB 数据时别把日志撑爆


class MatchAborted(Exception):
    """被用户中止。"""


@dataclass
class MatchConfig:
    seed: int = 0
    max_violations: int = 3          # 累计违规达到该值判负；0 表示一次即判负
    persistent_players: bool = True   # 玩家进程常驻（False = 每回合重启）
    retry_invalid: bool = False       # 协议类违规是否重试一次
    default_timeout_ms: int = 2000
    max_asks: int = MAX_TURNS     # 询问次数上限（防游戏不递增 turn 导致死循环）
    log_keep: int = 500           # data/logs 里保留最近多少份日志；0 = 不清理
    player_config: Dict[str, Any] = field(default_factory=dict)  # 下发给玩家的 config


@dataclass
class SeatStat:
    pid: str
    name: str
    answered: int = 0
    violations: int = 0
    total_ms: float = 0.0
    max_ms: int = 0
    disqualified: bool = False
    status: str = "正常"
    restarts: int = 0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.answered if self.answered else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid, "name": self.name, "answered": self.answered,
            "violations": self.violations, "avg_ms": round(self.avg_ms, 2),
            "max_ms": self.max_ms, "disqualified": self.disqualified,
            "status": self.status, "restarts": self.restarts,
        }


@dataclass
class MatchReport:
    match_id: str
    game_name: str = ""
    seed: int = 0
    turns: int = 0
    started_at: str = ""
    duration_ms: int = 0
    result: Optional[GameResult] = None
    seats: Dict[str, SeatStat] = field(default_factory=dict)
    disqualified: List[str] = field(default_factory=list)
    error: str = ""
    log_path: str = ""
    aborted: bool = False

    def ranking_with_status(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not self.result:
            return rows
        for idx, pid in enumerate(self.result.ranking, start=1):
            st = self.seats.get(pid)
            rows.append({
                "rank": idx,
                "pid": pid,
                "name": st.name if st else pid,
                "score": self.result.scores.get(pid, 0.0),
                "violations": st.violations if st else 0,
                "avg_ms": round(st.avg_ms, 2) if st else 0.0,
                "disqualified": pid in self.disqualified,
            })
        return rows


class MatchEngine:
    """一局对战的执行器。线程安全性：run() 在单独线程里跑，pause/step/stop 可从 UI 线程调用。"""

    def __init__(self, game_factory: Callable[[], GameAdapter],
                 seats: List[SeatConfig], config: MatchConfig,
                 hooks: Optional[Dict[str, Callable]] = None,
                 log_dir: str = ""):
        self.game_factory = game_factory
        self.seats = [s for s in seats if s.enabled]
        self.warnings: List[str] = []
        self.config = config
        self.hooks = hooks if hooks is not None else {}
        self.log_dir = log_dir
        self.match_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}"

        self.players: Dict[str, Any] = {}
        self.stats: Dict[str, SeatStat] = {}
        self._cancel = threading.Event()      # 停止/关窗时唤醒所有等待
        self._log_lock = threading.Lock()     # 并发询问时保护 seq 与日志文件
        self._asks = 0                        # 实际收到的 ask 次数（回合号的可靠来源）
        self._cond = threading.Condition()
        self._paused = False
        self._step_once = False
        self._stopped = False
        self._log_fp = None
        self._paused_s = 0.0
        self._seq = 0
        self._turn_logs: List[Dict[str, Any]] = []
        self._dedupe_pids()
        self.report = MatchReport(match_id=self.match_id, seed=config.seed)

    def _dedupe_pids(self) -> None:
        """手改 players.json 可能出现重复 pid：重复会让旧玩家对象被覆盖、子进程漏关。"""
        seen = set()
        unique = []
        for seat in self.seats:
            if seat.pid not in seen:
                seen.add(seat.pid)
                unique.append(seat)
                continue
            n = 2
            new_pid = f"{seat.pid}-{n}"
            while new_pid in seen:
                n += 1
                new_pid = f"{seat.pid}-{n}"
            self.warnings.append(f"玩家 id 重复：{seat.pid} → 已改名为 {new_pid}")
            clone = SeatConfig.from_dict({**seat.to_dict(), "pid": new_pid})
            seen.add(new_pid)
            unique.append(clone)
        self.seats = unique

    # -- 外部控制 ----------------------------------------------------------
    def pause(self) -> None:
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._step_once = False
            self._cond.notify_all()

    def step(self) -> None:
        with self._cond:
            self._step_once = True
            self._paused = True
            self._cond.notify_all()

    def stop(self) -> None:
        self._cancel.set()                    # 打断在途的 recv / 线程等待
        with self._cond:
            self._stopped = True
            self._paused = False
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _gate(self, turn: int) -> None:
        with self._cond:
            if self._paused and not self._step_once:
                t_wait = time.perf_counter()
                while self._paused and not self._step_once and not self._stopped:
                    self._cond.wait(timeout=0.15)
                self._paused_s += time.perf_counter() - t_wait
            if self._stopped:
                raise MatchAborted()
            if self._step_once:
                self._step_once = False

    # -- 日志 --------------------------------------------------------------
    def _emit(self, kind: str, text: str = "", turn: int = 0, player: str = "",
              data: Optional[Dict[str, Any]] = None) -> None:
        # 并发询问时多个线程会同时写日志，seq 与文件句柄必须串行
        with self._log_lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "kind": kind,
                "turn": turn,
                "player": player,
                "text": text,
            }
            if data is not None:
                entry["data"] = data
            if self._log_fp:
                try:
                    line = protocol.dumps(entry)
                    if len(line) > MAX_LOG_LINE:
                        # 动作里塞了超大结构时只留摘要，否则日志文件会被撑爆
                        entry["data"] = {"truncated": True, "bytes": len(line)}
                        entry["text"] = (str(entry.get("text", ""))
                                         + f" …（原始 {len(line) // 1024}KB，日志已截断）")[:500]
                        line = protocol.dumps(entry)
                    self._log_fp.write(line + "\n")
                    self._log_fp.flush()
                except Exception:
                    pass
            self._turn_logs.append(entry)
            if len(self._turn_logs) > 5000:
                del self._turn_logs[:2000]
        cb = self.hooks.get("on_log")
        if cb:
            cb(entry)

    def _hook(self, name: str, *args: Any) -> None:
        cb = self.hooks.get(name)
        if cb:
            try:
                cb(*args)
            except Exception:
                pass

    # -- 主流程 ------------------------------------------------------------
    def run(self) -> MatchReport:
        t_start = time.perf_counter()
        self.report.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
            self._prune_logs(self.config.log_keep)
            path = os.path.join(self.log_dir, f"{self.match_id}.jsonl")
            self.report.log_path = path
            try:
                self._log_fp = open(path, "w", encoding="utf-8")
            except Exception:
                self._log_fp = None

        for seat in self.seats:
            self.stats[seat.pid] = SeatStat(pid=seat.pid, name=seat.name)

        game: Optional[GameAdapter] = None
        try:
            game = self.game_factory()
            self._emit("system", f"对局开始：{len(self.seats)} 名玩家，种子 {self.config.seed}")
            for warn in self.warnings:
                self._emit("system", warn)
            player_infos = [{"id": s.pid, "name": s.name} for s in self.seats]
            state = game.start(player_infos, self.config.seed)
            self.report.game_name = game.name
            self._emit("system", f"游戏已就绪：{game.name}")

            self._start_players(game.name)

            turns = 0
            while True:
                if isinstance(state, GameResult):
                    self.report.result = state
                    break
                assert isinstance(state, Ask)
                # 不能只看游戏自报的 turn：写死 turn=1 的游戏永远触发不了上限
                self._asks += 1
                if self._asks > max(1, self.config.max_asks):
                    raise GameFault(
                        f"已询问 {self._asks} 次仍未结束（游戏可能没有递增 turn），疑似死循环")
                turns = max(turns, state.turn, self._asks)
                self._gate(state.turn)
                self._hook("on_turn", state)
                self._emit("turn", f"第 {state.turn} 回合开始（{len(state.requests)} 个请求）",
                           turn=state.turn)

                actions = self._collect(state)
                state = game.send_actions(state.turn, actions)

            self.report.turns = turns
            self._finish_players()
            self._emit("result", self._result_text())

        except (MatchAborted, PlayerAborted):
            self.report.aborted = True
            self.report.error = "已被手动中止"
            self._emit("system", "对局被手动中止")
        except GameFault as exc:
            self.report.error = f"游戏异常：{exc}"
            self._emit("error", self.report.error)
        except Exception as exc:  # noqa: BLE001
            self.report.error = f"引擎异常：{exc!r}"
            self._emit("error", self.report.error)
        finally:
            self._close_all(game)
            self.report.duration_ms = int(
                max(0.0, (time.perf_counter() - t_start) - self._paused_s) * 1000)
            self.report.seats = dict(self.stats)
            self.report.disqualified = [p for p, s in self.stats.items() if s.disqualified]
            if self._log_fp:
                try:
                    self._log_fp.write(protocol.dumps({
                        "kind": "report", "report": {
                            "match_id": self.match_id,
                            "turns": self.report.turns,
                            "duration_ms": self.report.duration_ms,
                            "error": self.report.error,
                            "result": self.report.result.__dict__ if self.report.result else None,
                            "seats": {k: v.to_dict() for k, v in self.stats.items()},
                        }}) + "\n")
                    self._log_fp.close()
                except Exception:
                    pass
                self._log_fp = None
            self._hook("on_finish", self.report)
        return self.report

    def _prune_logs(self, keep: int) -> None:
        """只保留最近 N 份日志，避免长期跑批量把磁盘堆满。"""
        if keep <= 0 or not self.log_dir:
            return
        try:
            files = [os.path.join(self.log_dir, f) for f in os.listdir(self.log_dir)
                     if f.endswith(".jsonl")]
            if len(files) < keep:
                return
            files.sort(key=os.path.getmtime)
            for path in files[:len(files) - keep + 1]:   # +1：马上要新建一份
                try:
                    os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    def _close_all(self, game: Optional[GameAdapter]) -> None:
        """并发关闭所有子进程。

        ProcessLink.close() 会先发 shutdown 再等 0.6s，串行关 5 个席位就是 3 秒，
        用户会觉得「点了停止没反应」。
        """
        targets: List[Any] = list(self.players.values())
        if game is not None:
            targets.append(game)
        if not targets:
            return

        def _one(obj: Any) -> None:
            try:
                obj.close()
            except Exception:
                pass

        try:
            with ThreadPoolExecutor(max_workers=min(16, len(targets)),
                                    thread_name_prefix="close") as pool:
                list(pool.map(_one, targets))
        except Exception:
            for obj in targets:
                _one(obj)

    # -- 玩家启动 ----------------------------------------------------------
    def _start_players(self, game_name: str) -> None:
        for seat in self.seats:
            st = self.stats[seat.pid]
            player = make_player(
                seat,
                persistent=self.config.persistent_players,
                on_log=lambda pid, text: self._emit("player", text, player=pid),
                on_raw=lambda pid, ch, text: self._emit("raw", f"[{ch}] {text}", player=pid),
            )
            self.players[seat.pid] = player
            try:
                player.start(game_name, self.config.player_config, cancel=self._cancel)
                self._emit("system", f"{seat.name} 已就绪（{seat.kind}）", player=seat.pid)
            except PlayerFault as fault:
                st.violations += 1
                st.status = f"启动失败：{fault.message}"
                self._emit("violation", f"{seat.name} 启动失败：{fault.message}", player=seat.pid)
                self._maybe_disqualify(seat.pid)

    def _maybe_disqualify(self, pid: str) -> None:
        st = self.stats.get(pid)
        if not st or st.disqualified:
            return
        limit = self.config.max_violations
        if limit <= 0 or st.violations >= limit:
            st.disqualified = True
            st.status = "判负"
            self._emit("violation", f"{st.name} 累计 {st.violations} 次违规，判负", player=pid)
            self._hook("on_disqualify", pid)

    # -- 收集动作 ----------------------------------------------------------
    def _collect(self, ask: Ask) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        # 同一回合里对同一玩家重复提问没有意义（动作本来就是按 pid 汇总的），
        # 而且并发下会让两个线程同时操作同一个进程，这里按 pid 去重
        reqs = []
        seen_pids = set()
        for req in ask.requests:
            if req.player in seen_pids:
                continue
            seen_pids.add(req.player)
            reqs.append(req)
        dup = len(ask.requests) - len(reqs)
        if dup > 0:
            self._emit("system",
                       f"第 {ask.turn} 回合有 {dup} 个重复请求，已按玩家去重（只问一次）",
                       turn=ask.turn)
        if not reqs:
            return results
        if len(reqs) == 1:
            self._collect_one(ask, reqs[0], results)
            return results
        # 同时制必须真并发：串行的话一个回合的墙钟时间 = Σ 每个人的 deadline，
        # 而且前一个人卡住会直接吃掉后面人的思考时间。
        # 上限放宽到 64：超过 16 人的游戏，后一批会晚开始，整回合被拖长
        with ThreadPoolExecutor(max_workers=min(64, len(reqs)),
                                thread_name_prefix="ask") as pool:
            futures = [pool.submit(self._collect_one, ask, req, results) for req in reqs]
            for fut in futures:
                fut.result()          # 让 MatchAborted / PlayerAborted 冒出来
        return results

    def _collect_one(self, ask: Ask, req: Any, results: Dict[str, Dict[str, Any]]) -> None:
        pid = req.player
        st = self.stats.get(pid)
        player = self.players.get(pid)
        if st is None or player is None:
            results[pid] = protocol.ActionResult(ok=False, error="unknown_player").to_dict()
            return
        if st.disqualified:
            results[pid] = protocol.ActionResult(
                ok=False, error="disqualified", violations=st.violations).to_dict()
            self._emit("action", "已判负，跳过", turn=ask.turn, player=pid,
                       data={"status": "skipped"})
            return
        if self._cancel.is_set():
            raise MatchAborted()
        try:
            protocol.dumps(req.view)
        except (TypeError, ValueError) as exc:
            # 游戏在 view 里塞了 NaN / set 之类：明确报「游戏异常」，别甩锅给引擎
            raise GameFault(
                f"第 {ask.turn} 回合：发给 {pid} 的 view 无法序列化为 JSON（{exc}）") from exc

        # 超时优先级：游戏给的值 > 界面上的「超时」默认值；席位超时只是**上限**，不能延长游戏给的时限。
        # 以前席位值会直接覆盖默认值，导致游戏不发 deadline_ms 时（如 sealed_bid）
        # 工具栏那个「超时」永远不生效——用户调了没反应。
        seat = next((s for s in self.seats if s.pid == pid), None)
        seat_cap = seat.timeout_ms if seat and seat.timeout_ms else 0
        deadline = req.deadline_ms or self.config.default_timeout_ms
        if deadline <= 0:                     # 配置写坏了别退化成 1ms
            deadline = seat_cap or 2000
        if seat_cap:
            deadline = min(deadline, seat_cap)
        deadline = max(1, int(deadline))

        ar = self._ask_one(player, st, ask.turn, req.view, deadline)
        results[pid] = ar.to_dict()
        self._hook("on_action", ask.turn, pid, ar)

    def _ask_one(self, player: Any, st: SeatStat, turn: int,
                 view: Dict[str, Any], deadline: int) -> protocol.ActionResult:
        attempts = 2 if self.config.retry_invalid else 1
        last_fault: Optional[PlayerFault] = None
        t_all = time.perf_counter()
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                action = player.act(turn, view, deadline, cancel=self._cancel)
                elapsed_f = (time.perf_counter() - t0) * 1000.0
                # 扣掉「重启进程 + 握手」的时间，常驻/非常驻两种模式的耗时才可比
                elapsed_f = max(0.0, elapsed_f - getattr(player, "last_setup_ms", 0.0))
                elapsed = int(round(elapsed_f))
                if action is None:
                    raise PlayerFault("protocol", "动作为空（null）")
                try:
                    protocol.dumps(action)      # allow_nan=False：NaN 在这里就会炸
                except (TypeError, ValueError) as exc:
                    raise PlayerFault("protocol", f"动作无法序列化为合法 JSON：{exc}") from exc
                st.answered += 1
                st.total_ms += elapsed_f
                st.max_ms = max(st.max_ms, elapsed)
                self._emit("action", "动作已接收", turn=turn, player=st.pid,
                           data={"status": "ok", "elapsed_ms": elapsed,
                                 "action": action, "view": view})
                return protocol.ActionResult(ok=True, action=action, elapsed_ms=elapsed,
                                             violations=st.violations)
            except PlayerAborted:
                raise MatchAborted()
            except PlayerFault as fault:
                last_fault = fault
                # 协议类错误且允许重试：同一回合内再问一次
                if attempt + 1 < attempts and fault.kind == "protocol" and not fault.fatal:
                    self._emit("violation", f"协议错误，重试：{fault.message}",
                               turn=turn, player=st.pid)
                    continue
                break

        assert last_fault is not None
        st.violations += 1
        st.status = f"{last_fault.kind}: {last_fault.message}"
        self._emit("violation", f"{last_fault.kind} — {last_fault.message}", turn=turn,
                   player=st.pid, data={"kind": last_fault.kind, "view": view})
        self._maybe_disqualify(st.pid)
        # 失败动作上报真实耗时，而不是拿 deadline 充数（否则游戏侧的超时分析全是错的）
        real_ms = int(round(max(0.0, (time.perf_counter() - t_all) * 1000
                               - getattr(player, "last_setup_ms", 0.0))))
        return protocol.ActionResult(ok=False, error=last_fault.kind,
                                     elapsed_ms=real_ms, violations=st.violations)

    # -- 收尾 --------------------------------------------------------------
    def _finish_players(self) -> None:
        result = self.report.result
        if not result:
            return
        for pid, player in self.players.items():
            st = self.stats.get(pid)
            you = {
                "rank": result.ranking.index(pid) + 1 if pid in result.ranking else None,
                "score": result.scores.get(pid, 0.0),
                "violations": st.violations if st else 0,
                "disqualified": st.disqualified if st else False,
            }
            try:
                player.finish(result, you)
            except Exception:
                pass

    def _result_text(self) -> str:
        r = self.report.result
        if not r:
            return "对局结束（无结果）"
        parts = []
        for idx, pid in enumerate(r.ranking, start=1):
            st = self.stats.get(pid)
            name = st.name if st else pid
            score = r.scores.get(pid, 0.0)
            parts.append(f"#{idx} {name}（{score:g}）")
        tail = f" · {r.reason}" if r.reason else ""
        return "最终排名：" + " > ".join(parts) + tail
