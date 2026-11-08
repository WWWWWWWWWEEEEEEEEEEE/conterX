"""游戏（上游程序）的接入适配层。

两种形态共用同一套「询问 / 收回动作」模型，所以引擎只需面对 GameAdapter：
- ProcessGame：游戏是独立进程，自己维护局面与回合循环，通过 NDJSON 和中转站对话；
- PluginGame：游戏是 Python 生成器函数，由中转站驱动（yield 询问，send 收回动作）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any, Callable, Dict, Generator, List, Optional

from . import protocol
from .protocol import Ask, GameResult, ProtocolError, Request
from .transport import (BadMessage, LinkClosed, LinkTimeout, ProcessLink,
                        build_command, split_args)


class GameFault(Exception):
    """游戏侧出错，无法继续对局。"""


# ---------------------------------------------------------------------------
# 插件式游戏的上下文
# ---------------------------------------------------------------------------
class GameContext:
    """传给插件游戏 run(ctx) 的上下文对象。

    用法：
        def run(ctx):
            actions = yield ctx.ask([{"player": "p1", "view": {...}}])
            ctx.log("第 1 回合结束")
            return {"ranking": ["p1", "p2"], "scores": {"p1": 3, "p2": 1}}
    """

    def __init__(self, players: List[Dict[str, str]], seed: int,
                 config: Dict[str, Any], log_fn: Callable[[str, str], None]):
        self.players = players
        self.seed = seed
        self.config = config
        self.turn = 0
        self._log = log_fn
        import random
        self.random = random.Random(seed)

    @property
    def player_ids(self) -> List[str]:
        return [p["id"] for p in self.players]

    def name_of(self, pid: str) -> str:
        for p in self.players:
            if p["id"] == pid:
                return p.get("name", pid)
        return pid

    def log(self, text: str, level: str = "info") -> None:
        self._log(str(text), level)

    def ask(self, requests: Any, deadline_ms: Optional[int] = None,
            note: str = "") -> Ask:
        """构造一次询问。requests 支持三种写法：
        - {"p1": view1, "p2": view2}
        - ["p1", "p2"]                       （所有人同一份公开视野）
        - [{"player": "p1", "view": {...}}, ...]
        """
        reqs: List[Request] = []
        if isinstance(requests, dict):
            for pid, view in requests.items():
                reqs.append(Request(player=str(pid), view=view or {}, deadline_ms=deadline_ms))
        elif isinstance(requests, (list, tuple)):
            for item in requests:
                if isinstance(item, str):
                    reqs.append(Request(player=item, view={"public": {}}, deadline_ms=deadline_ms))
                elif isinstance(item, dict):
                    pid = item.get("player") or item.get("player_id")
                    reqs.append(Request(player=str(pid), view=item.get("view") or {},
                                        deadline_ms=item.get("deadline_ms", deadline_ms)))
                else:
                    raise ValueError("ask 的 requests 元素必须是 dict 或 str")
        else:
            raise ValueError("ask 的 requests 必须是 dict 或 list")
        self.turn += 1
        return Ask(turn=self.turn, requests=reqs, note=note)


# ---------------------------------------------------------------------------
# 适配器基类
# ---------------------------------------------------------------------------
class GameAdapter:
    name: str = "未命名游戏"

    def start(self, players: List[Dict[str, str]], seed: int) -> Any:
        """返回 Ask 或 GameResult。"""
        raise NotImplementedError

    def send_actions(self, turn: int, actions: Dict[str, Dict[str, Any]]) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 进程式游戏
# ---------------------------------------------------------------------------
class ProcessGame(GameAdapter):
    def __init__(self, target: str, args: str = "", seed: int = 0,
                 config: Optional[Dict[str, Any]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 on_raw: Optional[Callable[[str, str], None]] = None):
        self.target = target
        self.args = args
        self.seed = seed
        self.config = config or {}
        self.on_log = on_log
        self.on_raw = on_raw
        self.link: Optional[ProcessLink] = None
        self.name = os.path.splitext(os.path.basename(target))[0]
        self._players: List[Dict[str, str]] = []
        self._last_turn = 0

    def start(self, players: List[Dict[str, str]], seed: int) -> Any:
        self._players = players
        self.seed = seed
        if not self.target or not os.path.exists(self.target):
            raise GameFault(f"游戏程序不存在：{self.target}")
        cmd = build_command(self.target, split_args(self.args))
        link = ProcessLink(
            cmd, name="game",
            cwd=os.path.dirname(os.path.abspath(self.target)) or None,
            on_raw=self.on_raw,
        )
        self.link = link
        try:
            # 同玩家侧：缺运行时时 Popen 抛 OSError，不能让整局变成「引擎异常」
            link.start()
            link.send(protocol.envelope(
                protocol.M_INIT,
                game_id=f"g{int(time.time())}",
                seed=int(seed),
                players=players,
                config=self.config,
            ))
            msg = link.recv(15.0, skip=self._skip_noise)
            try:
                mtype = protocol.check_envelope(msg)
            except ProtocolError as exc:
                raise GameFault(f"游戏协议错误：{exc}") from exc
            if mtype == protocol.M_ERROR:
                raise GameFault(f"游戏初始化报错：{msg.get('message', '')}")
            if mtype != protocol.M_READY:
                raise GameFault(f"初始化期望 ready，收到 {mtype}")
            self.name = str(msg.get("name") or self.name)
            link.send(protocol.envelope(protocol.M_RUN))
            return self._next()
        except OSError as exc:
            self.close()
            raise GameFault(
                f"无法启动游戏 {os.path.basename(self.target)}：{exc}") from exc
        except (LinkTimeout, LinkClosed, BadMessage) as exc:
            self.close()
            raise GameFault(f"游戏启动失败：{exc}") from exc

    def _skip_noise(self, msg: Dict[str, Any]) -> bool:
        if msg.get("type") == protocol.M_LOG:
            self._emit_log(msg)
            return True
        return False

    def _warn(self, text: str) -> None:
        self._emit_log({"text": f"⚠ {text}", "level": "warn"})

    def _emit_log(self, msg: Dict[str, Any]) -> None:
        text = str(msg.get("text", ""))
        if self.on_log:
            self.on_log(text, str(msg.get("level", "info")))
        else:
            # 没人接日志时（命令行自检等）打到 stderr，方便排查
            try:
                sys.stderr.write(f"[game] {text}\n")
            except Exception:
                pass

    def _next(self, timeout: float = 30.0) -> Any:
        assert self.link is not None
        try:
            msg = self.link.recv(timeout, skip=self._skip_noise)
        except LinkTimeout as exc:
            raise GameFault("等待游戏响应超时") from exc
        except (LinkClosed, BadMessage) as exc:
            raise GameFault(f"游戏进程异常：{exc}") from exc

        try:
            mtype = protocol.check_envelope(msg)
            if mtype == protocol.M_ASK:
                ask = protocol.parse_ask(msg, fallback_turn=self._last_turn + 1)
                self._last_turn = ask.turn
                return ask
            if mtype == protocol.M_OVER:
                return protocol.parse_result(msg, [p["id"] for p in self._players],
                                             on_warn=self._warn)
        except ProtocolError as exc:
            # 游戏自己发的报文不合法，不该算成「引擎异常」，否则排查方向全错
            raise GameFault(f"游戏协议错误：{exc}") from exc
        if mtype == protocol.M_ERROR:
            raise GameFault(f"游戏报错：{msg.get('message', '')}")
        raise GameFault(f"未预期的消息类型：{mtype}")

    def send_actions(self, turn: int, actions: Dict[str, Dict[str, Any]]) -> Any:
        if self.link is None:
            raise GameFault("游戏未启动")
        try:
            self.link.send(protocol.envelope(protocol.M_ACTIONS, turn=turn, actions=actions))
        except LinkClosed as exc:
            raise GameFault(f"写入游戏失败：{exc}") from exc
        return self._next()

    def close(self) -> None:
        if self.link:
            self.link.close()
            self.link = None


# ---------------------------------------------------------------------------
# 插件式游戏
# ---------------------------------------------------------------------------
def load_module_from_path(path: str, prefix: str = "conterx_mod") -> Any:
    if not path or not os.path.exists(path):
        raise GameFault(f"文件不存在：{path}")
    name = f"{prefix}_{abs(hash(os.path.abspath(path))) % 1000000}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GameFault(f"无法加载：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise GameFault(f"导入失败：{exc}") from exc
    return module


class PluginGame(GameAdapter):
    def __init__(self, target: str, seed: int = 0,
                 config: Optional[Dict[str, Any]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 on_raw: Optional[Callable[[str, str], None]] = None):
        self.target = target
        self.seed = seed
        self.config = config or {}
        self.on_log = on_log
        self.on_raw = on_raw
        self.name = os.path.splitext(os.path.basename(target))[0]
        self.gen: Optional[Generator] = None
        self.ctx: Optional[GameContext] = None

    def _build_game(self) -> Any:
        module = load_module_from_path(self.target, "conterx_game")
        for attr in ("create_game", "make_game", "GAME", "Game"):
            obj = getattr(module, attr, None)
            if obj is None:
                continue
            try:
                return obj() if callable(obj) else obj
            except Exception as exc:
                raise GameFault(f"创建游戏实例失败：{exc}") from exc
        raise GameFault("插件游戏必须提供 create_game() / GAME / class Game")

    def start(self, players: List[Dict[str, str]], seed: int) -> Any:
        self.seed = seed
        game = self._build_game()
        self.name = str(getattr(game, "name", self.name))
        run = getattr(game, "run", None)
        if not callable(run):
            raise GameFault("插件游戏必须实现 run(ctx) 生成器")
        self.ctx = GameContext(players, seed, self.config,
                               lambda t, lv="info": self.on_log(t, lv) if self.on_log else None)
        try:
            self.gen = run(self.ctx)
        except Exception as exc:
            raise GameFault(f"run(ctx) 调用失败：{exc}") from exc
        return self._advance(None, first=True)

    def _advance(self, value: Any, first: bool = False) -> Any:
        assert self.gen is not None
        try:
            nxt = next(self.gen) if first else self.gen.send(value)
        except StopIteration as stop:
            return self._to_result(stop.value)
        except Exception as exc:
            raise GameFault(f"游戏运行出错：{exc!r}") from exc
        return self._to_ask(nxt)

    def _to_ask(self, obj: Any) -> Ask:
        if isinstance(obj, Ask):
            if not obj.requests:
                raise GameFault("ctx.ask() 的 requests 不能为空（游戏会收到空动作）")
            # ctx.ask() 构造的 Ask 绕过了 parse_ask，这里补上与进程式一致的校验：
            # 否则插件游戏给模块玩家传个 list / 字符串也能一路发下去，和协议文档不符
            for req in obj.requests:
                if not isinstance(req.view, dict):
                    raise GameFault(
                        f"游戏协议错误：{req.player} 的 view 必须是对象"
                        f"（收到 {type(req.view).__name__}）")
            return obj
        if isinstance(obj, dict) and "requests" in obj:
            try:
                return protocol.parse_ask(obj, fallback_turn=(self.ctx.turn if self.ctx else 0))
            except ProtocolError as exc:
                # 手写报文不合法是游戏的问题，不能报成「引擎异常」
                raise GameFault(f"游戏协议错误：{exc}") from exc
        raise GameFault("run(ctx) 必须 yield ctx.ask(...) 的结果")

    def _warn(self, text: str) -> None:
        if self.on_log:
            self.on_log(f"⚠ {text}", "warn")

    def _to_result(self, value: Any) -> GameResult:
        pids = self.ctx.player_ids if self.ctx else []
        if value is None:
            raise GameFault("游戏结束但没有返回结果字典")
        if isinstance(value, GameResult):
            # 也要过一遍规范化：否则幽灵 pid / NaN 分数会从这条路径绕过去
            return protocol.parse_result(value.__dict__, pids, on_warn=self._warn)
        if isinstance(value, dict):
            return protocol.parse_result(value, pids, on_warn=self._warn)
        raise GameFault("游戏结束必须返回结果字典 {ranking, scores, ...}")

    def send_actions(self, turn: int, actions: Dict[str, Dict[str, Any]]) -> Any:
        return self._advance(actions)

    def close(self) -> None:
        if self.gen is not None:
            try:
                self.gen.close()
            except Exception:
                pass
            self.gen = None


def make_game(kind: str, target: str, args: str = "", seed: int = 0,
              config: Optional[Dict[str, Any]] = None,
              on_log: Optional[Callable[[str, str], None]] = None,
              on_raw: Optional[Callable[[str, str], None]] = None) -> GameAdapter:
    if kind == "plugin":
        return PluginGame(target, seed=seed, config=config, on_log=on_log, on_raw=on_raw)
    return ProcessGame(target, args=args, seed=seed, config=config, on_log=on_log, on_raw=on_raw)
