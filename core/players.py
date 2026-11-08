"""玩家（下游算法程序）的接入与管理。

支持两种形态，可在席位配置里切换：
- process：独立进程，stdin/stdout 走 NDJSON 协议，跨语言（py / exe / jar / js / bat ...）；
- module：单个 .py 文件，实现 decide(view, turn, ctx) 函数，进程内直接调用（调试方便）。
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import protocol
from .transport import (BadMessage, LinkCancelled, LinkClosed, LinkTimeout,
                        ProcessLink, build_command, split_args)


class PlayerAborted(Exception):
    """对局被中止，玩家动作没拿到（不算违规）。"""

SEAT_COLORS = ["#d9534f", "#4a90d9", "#4caf50", "#e8a33d", "#9b6bd6", "#3fb8af"]


@dataclass
class SeatConfig:
    """一个玩家席位的配置（可持久化到 data/config.json）。"""

    pid: str
    name: str
    kind: str = "process"          # process | module
    target: str = ""               # 程序路径
    args: str = ""                 # 额外命令行参数（字符串，按空格拆分）
    timeout_ms: int = 2000         # 单回合决策超时
    enabled: bool = True
    color: str = "#4a90d9"
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid, "name": self.name, "kind": self.kind,
            "target": self.target, "args": self.args,
            "timeout_ms": int(self.timeout_ms), "enabled": bool(self.enabled),
            "color": self.color, "note": self.note,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SeatConfig":
        def _int(v: Any, default: int) -> int:
            try:
                n = int(v)
                return n if n > 0 else default
            except (TypeError, ValueError):
                return default

        if not isinstance(d, dict):
            d = {}
        kind = d.get("kind")
        return SeatConfig(
            pid=str(d.get("pid") or "p1"),
            name=str(d.get("name") or d.get("pid") or "玩家"),
            kind=kind if kind in ("process", "module") else "process",
            target=str(d.get("target") or ""),
            args=str(d.get("args") or ""),
            timeout_ms=_int(d.get("timeout_ms"), 2000),
            enabled=bool(d.get("enabled", True)),
            color=str(d.get("color") or "#4a90d9"),
            note=str(d.get("note") or ""),
        )

    def command(self) -> List[str]:
        return build_command(self.target, split_args(self.args))


class PlayerFault(Exception):
    """玩家违规。kind: timeout | crash | protocol | illegal。"""

    def __init__(self, kind: str, message: str, fatal: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.fatal = fatal


class _ModuleCtx:
    """传给 module 型玩家的上下文。"""

    def __init__(self, pid: str, name: str, game: str, config: Dict[str, Any],
                 log_fn: Callable[[str], None]):
        self.player_id = pid
        self.name = name
        self.game = game
        self.config = config
        self._log = log_fn

    def log(self, text: str) -> None:
        self._log(str(text))


class BasePlayer:
    """玩家适配器基类。"""

    def __init__(self, seat: SeatConfig,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 on_raw: Optional[Callable[[str, str, str], None]] = None):
        self.seat = seat
        self.on_log = on_log          # (pid, text)
        self.on_raw = on_raw          # (pid, channel, text)
        self.start_count = 0
        # 本次 act 里花在「启动进程 + 握手」上的毫秒数。
        # 非常驻模式下每回合都要重启，这段时间不该算进「决策耗时」，否则和常驻模式不可比。
        self.last_setup_ms = 0.0

    # -- 子类实现 ----------------------------------------------------------
    def start(self, game_name: str, config: Dict[str, Any], start_timeout: float = 15.0,
              cancel: Optional[threading.Event] = None) -> None:
        raise NotImplementedError

    def act(self, turn: int, view: Dict[str, Any], deadline_ms: int,
            cancel: Optional[threading.Event] = None) -> Any:
        """返回动作对象；违规时抛 PlayerFault，被中止时抛 PlayerAborted。"""
        raise NotImplementedError

    def finish(self, result: protocol.GameResult, you: Dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass

    def _log(self, text: str) -> None:
        if self.on_log and text:
            self.on_log(self.seat.pid, text)


class ProcessPlayer(BasePlayer):
    """独立进程玩家：常驻或每回合重启均可。"""

    def __init__(self, seat: SeatConfig, persistent: bool = True, **kw: Any):
        super().__init__(seat, **kw)
        self.persistent = persistent
        self.link: Optional[ProcessLink] = None
        self._game_name = ""
        self._config: Dict[str, Any] = {}

    # -- 生命周期 ----------------------------------------------------------
    def start(self, game_name: str, config: Dict[str, Any], start_timeout: float = 15.0,
              cancel: Optional[threading.Event] = None) -> None:
        self._game_name = game_name
        self._config = config
        self.close()
        if not self.seat.target or not os.path.exists(self.seat.target):
            raise PlayerFault("crash", f"程序不存在：{self.seat.target}", fatal=True)
        cmd = self.seat.command()
        link = ProcessLink(
            cmd,
            name=self.seat.pid,
            cwd=os.path.dirname(os.path.abspath(self.seat.target)) or None,
            on_log=None,
            on_raw=(lambda ch, txt: self.on_raw(self.seat.pid, ch, txt)) if self.on_raw else None,
        )
        self.link = link
        try:
            # Popen 必须在 try 里：没装 node / java、或没有执行权限时是 OSError，
            # 否则会一路冒到引擎兜底，变成「引擎异常」且一条违规都不记
            link.start()
            self.start_count += 1
            link.send(protocol.envelope(
                protocol.M_INIT,
                player_id=self.seat.pid,
                name=self.seat.name,
                game={"name": game_name},
                config=config,
                persistent=self.persistent,
            ))
            msg = link.recv(start_timeout, skip=self._skip_noise, cancel=cancel)
            mtype = protocol.check_envelope(msg)
            if mtype == protocol.M_ERROR:
                raise PlayerFault("crash", f"初始化报错：{msg.get('message', '')}", fatal=True)
            if mtype not in (protocol.M_READY, protocol.M_ACTION):
                raise PlayerFault("protocol", f"初始化期望 ready，收到 {mtype}", fatal=True)
        except LinkCancelled:
            self.close()
            raise PlayerAborted()
        except OSError as exc:
            self.close()
            raise PlayerFault(
                "crash", f"无法启动 {os.path.basename(self.seat.target)}：{exc}", fatal=True) from exc
        except protocol.ProtocolError as exc:
            self.close()
            raise PlayerFault("protocol", f"初始化报文不合法：{exc}", fatal=True) from exc
        except (LinkTimeout, LinkClosed, BadMessage) as exc:
            self.close()
            raise PlayerFault("crash", f"启动失败：{exc}", fatal=True) from exc

    def _skip_noise(self, msg: Dict[str, Any]) -> bool:
        mtype = msg.get("type")
        if mtype == protocol.M_LOG:
            self._log(str(msg.get("text", "")))
            return True
        return False

    # -- 单回合 ------------------------------------------------------------
    def act(self, turn: int, view: Dict[str, Any], deadline_ms: int,
            cancel: Optional[threading.Event] = None) -> Any:
        self.last_setup_ms = 0.0
        t_setup = time.perf_counter()
        if self.persistent:
            if self.link is None or not self.link.alive:
                self.start(self._game_name, self._config, cancel=cancel)
            link = self.link
        else:
            # 非常驻：每回合一个新进程。start() 会先关掉上一回合的进程，
            # 所以这里**不**在 act 结束时关闭——否则对局结束时游戏发的 over 永远送不到。
            self.start(self._game_name, self._config, cancel=cancel)
            link = self.link
        self.last_setup_ms = (time.perf_counter() - t_setup) * 1000.0
        assert link is not None

        timeout = max(0.05, deadline_ms / 1000.0)
        try:
            link.send(protocol.build_turn_message(
                turn, view, deadline_ms, game_name=self._game_name))
            msg = link.recv(timeout, skip=self._skip_noise, cancel=cancel)
            mtype = protocol.check_envelope(msg)
            if mtype == protocol.M_ERROR:
                raise PlayerFault("protocol", f"玩家报错：{msg.get('message', '')}")
            if mtype != protocol.M_ACTION:
                raise PlayerFault("protocol", f"期望 action，收到 {mtype}")
            # 回合号必须对得上：对不上说明玩家多吐了一条动作，队列已经错位
            got = msg.get("turn")
            if got is not None:
                try:
                    got = int(got)
                except (TypeError, ValueError):
                    got = None
                if got is not None and got != turn:
                    self.close()
                    raise PlayerFault(
                        "protocol", f"回合号错位：期望第 {turn} 回合，收到第 {got} 回合的动作",
                        fatal=True)
            return protocol.parse_action(msg)
        except LinkCancelled:
            raise PlayerAborted()
        except LinkTimeout as exc:
            self.close()
            raise PlayerFault("timeout", f"决策超时（>{deadline_ms}ms）", fatal=True) from exc
        except (LinkClosed, BadMessage) as exc:
            self.close()
            raise PlayerFault("crash", f"进程异常：{exc}", fatal=True) from exc
        except protocol.ProtocolError as exc:
            # 协议错误后队列里可能还有残留，清掉，避免污染下一回合
            if link:
                link.drain()
            raise PlayerFault("protocol", str(exc)) from exc

    def finish(self, result: protocol.GameResult, you: Dict[str, Any]) -> None:
        if self.link and self.link.alive:
            try:
                self.link.send(protocol.build_over_message(result, you))
            except Exception:
                pass

    def close(self) -> None:
        if self.link:
            self.link.close()
            self.link = None


class ModulePlayer(BasePlayer):
    """单文件 Python 玩家：实现 decide(view, turn, ctx) 即可。"""

    def __init__(self, seat: SeatConfig, persistent: bool = True, **kw: Any):
        super().__init__(seat, **kw)
        self.persistent = persistent
        self.module: Any = None
        self.ctx: Optional[_ModuleCtx] = None
        self.broken = False        # 上一回合超时过：本局不再调用（线程杀不掉，避免并发改全局）

    def _load(self) -> None:
        path = self.seat.target
        if not path or not os.path.exists(path):
            raise PlayerFault("crash", f"文件不存在：{path}", fatal=True)
        name = f"conterx_player_{self.seat.pid}_{abs(hash(path)) % 100000}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise PlayerFault("crash", f"无法加载：{path}", fatal=True)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PlayerFault("crash", f"导入失败：{exc}", fatal=True) from exc
        if not hasattr(module, "decide"):
            raise PlayerFault("crash", "缺少 decide(view, turn, ctx) 函数", fatal=True)
        self.module = module

    def start(self, game_name: str, config: Dict[str, Any], start_timeout: float = 15.0,
              cancel: Optional[threading.Event] = None) -> None:
        self.module = None
        self._load()
        self.start_count += 1
        self.ctx = _ModuleCtx(self.seat.pid, self.seat.name, game_name, config, self._log)
        setup = getattr(self.module, "setup", None)
        if callable(setup):
            try:
                setup(self.ctx)
            except Exception as exc:
                raise PlayerFault("crash", f"setup 失败：{exc}", fatal=True) from exc

    def act(self, turn: int, view: Dict[str, Any], deadline_ms: int,
            cancel: Optional[threading.Event] = None) -> Any:
        if self.broken:
            raise PlayerFault(
                "timeout", "模块式玩家上一回合已超时（线程无法终止），本局不再询问")
        self.last_setup_ms = 0.0
        t_setup = time.perf_counter()
        if not self.persistent or self.module is None:
            self.start(self.ctx.game if self.ctx else "", self.ctx.config if self.ctx else {})
        self.last_setup_ms = (time.perf_counter() - t_setup) * 1000.0
        assert self.module is not None and self.ctx is not None
        box: Dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = self.module.decide(view, turn, self.ctx)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        th = threading.Thread(target=run, daemon=True)
        th.start()
        limit = max(0.05, deadline_ms / 1000.0)
        waited = 0.0
        while th.is_alive() and waited < limit:
            if cancel is not None and cancel.is_set():
                raise PlayerAborted()
            th.join(0.05)
            waited += 0.05
        if th.is_alive():
            self.broken = True
            raise PlayerFault("timeout", f"决策超时（>{deadline_ms}ms，进程内线程无法强制终止）")
        if "error" in box:
            raise PlayerFault("protocol", f"decide 抛异常：{box['error']!r}")
        if "value" not in box:
            raise PlayerFault("protocol", "decide 没有返回值")
        return box["value"]

    def finish(self, result: protocol.GameResult, you: Dict[str, Any]) -> None:
        teardown = getattr(self.module, "teardown", None) if self.module else None
        if callable(teardown):
            try:
                teardown(result, you)
            except Exception:
                pass

    def close(self) -> None:
        self.module = None
        self.ctx = None


def make_player(seat: SeatConfig, persistent: bool = True,
                on_log: Optional[Callable[[str, str], None]] = None,
                on_raw: Optional[Callable[[str, str, str], None]] = None) -> BasePlayer:
    if seat.kind == "module":
        return ModulePlayer(seat, persistent=persistent, on_log=on_log, on_raw=on_raw)
    return ProcessPlayer(seat, persistent=persistent, on_log=on_log, on_raw=on_raw)
