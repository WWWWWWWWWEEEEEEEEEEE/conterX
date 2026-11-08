"""conterX 协议层：消息格式、数据结构与校验。

协议版本：conterx/1（每条消息都带 "v": 1）
传输方式：stdin/stdout 上的 NDJSON —— 一行一个 JSON 对象，UTF-8。

本模块不依赖任何 GUI，可单独被游戏 / 玩家程序引用。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
PROTOCOL_TAG = "conterx/1"

# conterx/2：面向大模型 / 智能体接入的增量版本。
# 只新增字段、不改 conterx/1 语义；v:1 消息的所有处理路径保持逐字不变。
# 详见同目录 AGENT-PROTOCOL.md。
PROTOCOL_VERSION_V2 = 2
PROTOCOL_TAG_V2 = "conterx/2"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION, PROTOCOL_VERSION_V2)

# ---------------------------------------------------------------------------
# 消息类型常量
# ---------------------------------------------------------------------------
# 中转站 -> 游戏
M_INIT = "init"
M_RUN = "run"
M_ACTIONS = "actions"
M_SHUTDOWN = "shutdown"
# 游戏 -> 中转站
M_READY = "ready"
M_ASK = "ask"
M_LOG = "log"
M_OVER = "over"
M_ERROR = "error"

# 中转站 -> 玩家
M_TURN = "turn"
# 玩家 -> 中转站
M_ACTION = "action"


class ProtocolError(Exception):
    """消息不符合协议。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Request:
    """游戏向某一位玩家索取一次动作。"""

    player: str
    view: Dict[str, Any] = field(default_factory=dict)
    deadline_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"player": self.player, "view": self.view}
        if self.deadline_ms:
            d["deadline_ms"] = int(self.deadline_ms)
        return d


@dataclass
class Ask:
    """一次询问：可以只问一个人（顺序制），也可以同时问多个人（同时制）。"""

    turn: int
    requests: List[Request] = field(default_factory=list)
    note: str = ""

    def players(self) -> List[str]:
        return [r.player for r in self.requests]


@dataclass
class GameResult:
    """对局结果。ranking 为从第一名到最后一名的玩家 id 列表。"""

    ranking: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    winner: Optional[str] = None
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """某位玩家某回合的动作结果（会原样回传给游戏）。"""

    ok: bool
    action: Any = None
    elapsed_ms: int = 0
    error: str = ""
    violations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"ok": self.ok, "elapsed_ms": int(self.elapsed_ms)}
        if self.ok:
            d["action"] = self.action
        else:
            d["error"] = self.error or "unknown"
            d["action"] = None
        if self.violations:
            d["violations"] = int(self.violations)
        return d


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def dumps(obj: Any) -> str:
    # allow_nan=False：NaN/Infinity 不是合法 JSON，别的语言解析器会直接报错
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def loads(line: str) -> Dict[str, Any]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    return obj


def envelope(msg_type: str, **fields: Any) -> Dict[str, Any]:
    """构造一条带版本号的消息。"""
    msg = {"v": PROTOCOL_VERSION, "type": msg_type}
    msg.update(fields)
    return msg


# ---------------------------------------------------------------------------
# conterx/2 增量部分（纯新增；不修改上面任何 conterx/1 函数）
# ---------------------------------------------------------------------------
# 智能体扩展字段。全部是可选增量：不带这些字段的 v:2 消息与 v:1 消息等价。
# 中转站把它们当作不透明 JSON 透传，只有 Hub / 智能体程序关心其含义。
AGENT_TURN_FIELDS = (
    "persona",        # 该席位的人设 / 系统提示词
    "messages",       # 群聊式历史（[{role, name, text, kind}]）
    "schema",         # 期望动作的 JSON Schema
    "session_id",     # 跨回合会话记忆标识
    "phase",          # 游戏阶段（如 狼人杀: 夜晚 / 白天 / 投票）
    "rules",          # 游戏规则摘要
)
AGENT_ACTION_FIELDS = (
    "say",            # 公开发言（由游戏广播给所有玩家）
    "reasoning",      # 思维过程（只进日志 / 观战面板，不给其他玩家）
    "usage",          # {"prompt_tokens", "completion_tokens"}
    "model",          # 实际使用的模型 id
)


def envelope_v2(msg_type: str, **fields: Any) -> Dict[str, Any]:
    """构造一条 conterx/2 消息。conterx/1 的 envelope() 保持原样不动。"""
    msg = {"v": PROTOCOL_VERSION_V2, "type": msg_type}
    msg.update(fields)
    return msg


def agent_meta(msg: Dict[str, Any], fields=AGENT_TURN_FIELDS) -> Dict[str, Any]:
    """从消息里挑出智能体扩展字段（用于日志 / 观战 / Hub 转发）。

    纯读取，不做校验、不改原消息；缺字段就跳过。
    """
    return {k: msg[k] for k in fields if k in msg}


def check_envelope(msg: Dict[str, Any]) -> str:
    """校验信封并返回 type。"""
    if not isinstance(msg, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError("缺少 type 字段")
    ver = msg.get("v", PROTOCOL_VERSION)
    if isinstance(ver, bool):
        raise ProtocolError("v 字段必须是整数，收到布尔值")
    if isinstance(ver, float) and ver.is_integer():
        ver = int(ver)
    elif isinstance(ver, str):
        try:
            ver = int(ver.strip())
        except ValueError:
            raise ProtocolError(f"v 字段必须是整数，收到 {ver!r}") from None
    if not isinstance(ver, int):
        raise ProtocolError(f"v 字段必须是整数，收到 {ver!r}")
    if ver < 1:
        raise ProtocolError(f"协议版本 {ver} 无效（最低支持 1）")
    if ver not in SUPPORTED_VERSIONS:
        supported = "/".join(str(v) for v in SUPPORTED_VERSIONS)
        raise ProtocolError(f"协议版本 {ver} 不受支持（本机支持 {supported}）")
    return mtype


# ---------------------------------------------------------------------------
# 游戏消息解析
# ---------------------------------------------------------------------------
def parse_ask(msg: Dict[str, Any], fallback_turn: int = 0) -> Ask:
    turn = msg.get("turn", fallback_turn)
    if not isinstance(turn, int):
        try:
            turn = int(turn)
        except (TypeError, ValueError):
            turn = fallback_turn
    raw = msg.get("requests")
    if not isinstance(raw, list) or not raw:
        raise ProtocolError("ask 消息必须包含非空的 requests 列表")
    reqs: List[Request] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProtocolError("requests 中的每一项必须是对象")
        pid = item.get("player") or item.get("player_id")
        if not isinstance(pid, str) or not pid:
            raise ProtocolError("request 缺少 player 字段")
        view = item.get("view", {})
        if not isinstance(view, dict):
            raise ProtocolError(f"{pid} 的 view 必须是对象")
        dl = item.get("deadline_ms")
        if dl is not None:
            if isinstance(dl, bool) or not isinstance(dl, (int, float)):
                raise ProtocolError(f"{pid} 的 deadline_ms 必须是数字")
            if dl != dl or dl in (float("inf"), float("-inf")):
                raise ProtocolError(f"{pid} 的 deadline_ms 必须是有限数字")
            if dl < 0:
                # 负数会被算成 50ms，让跑得飞快的玩家集体判负——必须挡住
                raise ProtocolError(
                    f"{pid} 的 deadline_ms 不能是负数（收到 {dl}）；"
                    f"想用默认值请省略该字段或传 0")
            dl = int(dl) if dl > 0 else None      # 0 = 用默认值
        reqs.append(Request(player=pid, view=view, deadline_ms=dl))
    return Ask(turn=turn, requests=reqs, note=str(msg.get("note", "")))


def parse_result(msg: Dict[str, Any], player_ids: List[str],
                 on_warn: Optional[Any] = None) -> GameResult:
    # 允许两种写法：字段直接放在顶层，或包在 "result" 里
    if isinstance(msg.get("result"), dict):
        msg = msg["result"]
    raw_rank = msg.get("ranking")
    scores_raw = msg.get("scores") or {}
    if not isinstance(scores_raw, dict):
        raise ProtocolError("result.scores 必须是对象")

    def warn(text: str) -> None:
        if on_warn:
            try:
                on_warn(text)
            except Exception:
                pass

    known = set(player_ids)
    scores: Dict[str, float] = {}
    for k, v in scores_raw.items():
        pid = str(k)
        try:
            val = float(v)
        except (TypeError, ValueError):
            val = 0.0
        if not math.isfinite(val):
            # NaN/Infinity 会让日志里的 report 行序列化失败、静默丢失
            warn(f"分数 {pid}={v!r} 不是有限数，按 0 处理")
            val = 0.0
        scores[pid] = val

    if isinstance(raw_rank, list) and raw_rank:
        ranking = []
        for x in raw_rank:
            pid = str(x)
            if pid not in known:
                # 幽灵 pid 会把所有人的名次挤后一位，批量的平均名次全错
                warn(f"排名里的 {pid!r} 不是本局玩家，已忽略")
                continue
            if pid not in ranking:      # 去重：重复 pid 会让结算表出现两行同名
                ranking.append(pid)
    else:
        # 未给排名时按分数降序推断
        ranking = sorted(player_ids, key=lambda p: scores.get(p, 0.0), reverse=True)

    # 补齐未出现在 ranking 里的玩家，放到末尾
    for pid in player_ids:
        if pid not in ranking:
            ranking.append(pid)

    winner = msg.get("winner")
    if not isinstance(winner, str) or not winner:
        winner = ranking[0] if ranking else None

    extra = msg.get("extra")
    return GameResult(
        ranking=ranking,
        scores=scores,
        winner=winner,
        reason=str(msg.get("reason", "")),
        extra=extra if isinstance(extra, dict) else {},
    )


# ---------------------------------------------------------------------------
# 玩家消息解析
# ---------------------------------------------------------------------------
def parse_action(msg: Dict[str, Any]) -> Any:
    if "action" not in msg:
        raise ProtocolError("action 消息缺少 action 字段")
    return msg["action"]


def build_turn_message(turn: int, view: Dict[str, Any], deadline_ms: int,
                       game_name: str = "") -> Dict[str, Any]:
    return envelope(
        M_TURN,
        turn=turn,
        deadline_ms=int(deadline_ms),
        view=view,
        game=game_name,
    )


def build_over_message(result: GameResult, you: Dict[str, Any]) -> Dict[str, Any]:
    return envelope(
        M_OVER,
        result={
            "ranking": result.ranking,
            "scores": result.scores,
            "winner": result.winner,
            "reason": result.reason,
        },
        you=you,
    )
