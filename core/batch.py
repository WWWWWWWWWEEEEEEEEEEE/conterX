"""批量对抗：连续跑 N 局，汇总胜率 / 平均名次 / 违规率，输出排行榜与 CSV。

用于「算法对抗」的评测场景：同一批玩家、同一款游戏，换种子反复对局。
"""

from __future__ import annotations

import csv
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .engine import MatchConfig, MatchEngine, MatchReport
from .games import GameAdapter
from .players import SeatConfig


@dataclass
class BatchConfig:
    rounds: int = 20
    seed_base: int = 1


@dataclass
class PlayerAgg:
    pid: str
    name: str
    matches: int = 0
    wins: int = 0
    top2: int = 0
    score_sum: float = 0.0
    rank_sum: int = 0
    violations: int = 0
    dq: int = 0
    answered: int = 0
    ms_sum: int = 0

    def to_row(self, rank: int) -> Dict[str, Any]:
        return {
            "rank": rank,
            "pid": self.pid,
            "name": self.name,
            "matches": self.matches,
            "wins": self.wins,
            "win_rate": round(self.wins / self.matches * 100, 1) if self.matches else 0.0,
            "top2_rate": round(self.top2 / self.matches * 100, 1) if self.matches else 0.0,
            "avg_rank": round(self.rank_sum / self.matches, 2) if self.matches else 0.0,
            "avg_score": round(self.score_sum / self.matches, 2) if self.matches else 0.0,
            "violations": self.violations,
            "dq": self.dq,
            "avg_ms": round(self.ms_sum / self.answered, 1) if self.answered else 0.0,
        }


@dataclass
class BatchReport:
    rounds: int = 0
    finished: int = 0
    stopped: bool = False
    rows: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    csv_path: str = ""
    started_at: str = ""
    duration_ms: int = 0


class BatchRunner:
    def __init__(self, game_factory: Callable[[], GameAdapter],
                 seats: List[SeatConfig], config: MatchConfig, batch: BatchConfig,
                 hooks: Optional[Dict[str, Callable]] = None,
                 log_dir: str = "", batch_dir: str = ""):
        self.game_factory = game_factory
        self.seats = seats
        self.config = config
        self.batch = batch
        self.hooks = hooks if hooks is not None else {}
        self.log_dir = log_dir
        self.batch_dir = batch_dir
        self._stop = threading.Event()
        self._current_engine: Optional[MatchEngine] = None
        self.report = BatchReport(rounds=batch.rounds)

    def stop(self) -> None:
        self._stop.set()
        eng = self._current_engine
        if eng is not None:          # 正在跑的那一局也要立刻停
            eng.stop()

    def _hook(self, name: str, *args: Any) -> None:
        cb = self.hooks.get(name)
        if cb:
            try:
                cb(*args)
            except Exception:
                pass

    def run(self) -> BatchReport:
        t0 = time.perf_counter()
        self.report.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        aggs: Dict[str, PlayerAgg] = {
            s.pid: PlayerAgg(pid=s.pid, name=s.name) for s in self.seats if s.enabled
        }

        for i in range(self.batch.rounds):
            if self._stop.is_set():
                self.report.stopped = True
                break
            seed = self.batch.seed_base + i
            cfg = MatchConfig(
                seed=seed,
                max_violations=self.config.max_violations,
                persistent_players=self.config.persistent_players,
                retry_invalid=self.config.retry_invalid,
                default_timeout_ms=self.config.default_timeout_ms,
                player_config=self.config.player_config,
            )
            engine = MatchEngine(self.game_factory, self.seats, cfg,
                                 hooks={"on_log": self.hooks.get("on_match_log")} if self.hooks else None,
                                 log_dir=self.log_dir)
            self._current_engine = engine
            try:
                report = engine.run()
            finally:
                self._current_engine = None
            self.report.finished = i + 1
            if report.error and not report.aborted:
                self.report.errors.append(f"第 {i + 1} 局：{report.error}")
            self._accumulate(aggs, report)
            self._hook("on_progress", i + 1, self.batch.rounds, report)

        rows = [a.to_row(0) for a in aggs.values()]
        rows.sort(key=lambda r: (-r["wins"], -r["avg_score"], r["avg_rank"]))
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx
        self.report.rows = rows
        self.report.duration_ms = int((time.perf_counter() - t0) * 1000)
        if self.batch_dir:
            self.report.csv_path = self._write_csv(rows)
        self._hook("on_finish", self.report)
        return self.report

    def _accumulate(self, aggs: Dict[str, PlayerAgg], report: MatchReport) -> None:
        result = report.result
        if not result:
            return
        for pid, agg in aggs.items():
            agg.matches += 1
            st = report.seats.get(pid)
            if st:
                agg.violations += st.violations
                agg.answered += st.answered
                agg.ms_sum += st.total_ms
            if pid in report.disqualified:
                agg.dq += 1
            if pid in result.ranking:
                pos = result.ranking.index(pid) + 1
                agg.rank_sum += pos
                if pos == 1 and pid not in report.disqualified:
                    agg.wins += 1
                if pos <= 2:
                    agg.top2 += 1
            agg.score_sum += result.scores.get(pid, 0.0)

    def _write_csv(self, rows: List[Dict[str, Any]]) -> str:
        os.makedirs(self.batch_dir, exist_ok=True)
        stamp = datetime.now().strftime("batch-%Y%m%d-%H%M%S-%f")[:-3]
        path = os.path.join(self.batch_dir, f"{stamp}.csv")
        n = 2
        while os.path.exists(path):          # 同一毫秒内跑两次也不覆盖
            path = os.path.join(self.batch_dir, f"{stamp}-{n}.csv")
            n += 1
        headers = ["rank", "name", "pid", "matches", "wins", "win_rate", "top2_rate",
                   "avg_rank", "avg_score", "violations", "dq", "avg_ms"]
        cn = {"rank": "名次", "name": "玩家", "pid": "编号", "matches": "局数",
              "wins": "胜场", "win_rate": "胜率%", "top2_rate": "前二%",
              "avg_rank": "平均名次", "avg_score": "平均得分",
              "violations": "违规", "dq": "判负", "avg_ms": "平均耗时ms"}
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow([cn[h] for h in headers])
                for row in rows:
                    w.writerow([row.get(h, "") for h in headers])
        except Exception:
            return ""
        return path
