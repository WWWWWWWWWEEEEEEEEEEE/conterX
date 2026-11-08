"""配置与数据的本地持久化（全部放在 data/ 下，纯 JSON）。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .players import SEAT_COLORS, SeatConfig

DEFAULT_PLAYERS = [
    {"pid": "p1", "name": "玩家1", "color": SEAT_COLORS[0]},
    {"pid": "p2", "name": "玩家2", "color": SEAT_COLORS[1]},
    {"pid": "p3", "name": "玩家3", "color": SEAT_COLORS[2]},
]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_timeout_ms": 2000,
    "max_violations": 3,
    "persistent_players": True,
    "retry_invalid": False,
    "seed": 1,
    "batch_rounds": 20,
    "game": {"kind": "process", "target": "", "args": "", "config": {}},
    "recent_games": [],
    "recent_players": [],
}


class Store:
    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        self.data_dir = os.path.join(self.base_dir, "data")
        self.log_dir = os.path.join(self.data_dir, "logs")
        self.batch_dir = os.path.join(self.data_dir, "batch")
        for d in (self.data_dir, self.log_dir, self.batch_dir):
            os.makedirs(d, exist_ok=True)
        self.players_path = os.path.join(self.data_dir, "players.json")
        self.settings_path = os.path.join(self.data_dir, "settings.json")

    # -- 通用 --------------------------------------------------------------
    def _read(self, path: str, fallback: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return fallback

    def _write(self, path: str, obj: Any) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # -- 玩家席位 ----------------------------------------------------------
    def _default_players(self) -> List[Dict[str, Any]]:
        """首次运行时，把示例玩家直接填进席位，开箱即用。"""
        picks = [("bid_random.py", "玩家1"), ("bid_greedy.py", "玩家2"), ("bid_shade.py", "玩家3")]
        ex = os.path.join(self.base_dir, "examples", "players")
        out: List[Dict[str, Any]] = []
        for i, (fn, name) in enumerate(picks):
            path = os.path.join(ex, fn)
            out.append({
                "pid": f"p{i + 1}", "name": name, "kind": "process",
                "target": path if os.path.exists(path) else "",
                "color": SEAT_COLORS[i % len(SEAT_COLORS)],
            })
        return out

    def load_players(self) -> List[SeatConfig]:
        raw = self._read(self.players_path, None)
        if not isinstance(raw, list) or not raw:
            raw = self._default_players()
        seats: List[SeatConfig] = []
        for i, item in enumerate(raw):
            try:
                if isinstance(item, str):
                    item = {"pid": item, "name": item}
                if not isinstance(item, dict):
                    continue
                item = dict(item)
                item.setdefault("color", SEAT_COLORS[i % len(SEAT_COLORS)])
                seats.append(SeatConfig.from_dict(item))
            except Exception:
                continue          # 单条坏了就跳过，别让整个程序起不来
        if not seats:
            seats = [SeatConfig.from_dict(d) for d in self._default_players()]
        return seats

    def save_players(self, seats: List[SeatConfig]) -> None:
        self._write(self.players_path, [s.to_dict() for s in seats])

    # -- 设置 --------------------------------------------------------------
    def load_settings(self) -> Dict[str, Any]:
        raw = self._read(self.settings_path, {})
        settings = dict(DEFAULT_SETTINGS)
        if not isinstance(raw, dict):
            return settings
        for key, default in DEFAULT_SETTINGS.items():
            if key not in raw:
                continue
            val = raw[key]
            if isinstance(default, bool):
                settings[key] = val if isinstance(val, bool) else default
            elif isinstance(default, int):
                try:
                    settings[key] = int(val)
                except (TypeError, ValueError):
                    settings[key] = default
            elif isinstance(default, dict):
                settings[key] = val if isinstance(val, dict) else dict(default)
            elif isinstance(default, str):
                settings[key] = val if isinstance(val, str) else default
        game = settings.get("game")
        if isinstance(game, dict):
            settings["game"] = {
                "kind": game.get("kind") if game.get("kind") in ("process", "plugin") else "process",
                "target": str(game.get("target") or ""),
                "args": str(game.get("args") or ""),
                "config": game.get("config") if isinstance(game.get("config"), dict) else {},
            }
        if not isinstance(settings.get("recent_games"), list):
            settings["recent_games"] = []
        return settings

    def save_settings(self, settings: Dict[str, Any]) -> None:
        self._write(self.settings_path, settings)

    def remember_game(self, kind: str, target: str, args: str) -> None:
        s = self.load_settings()
        recent = [g for g in s.get("recent_games", [])
                  if not (g.get("target") == target and g.get("kind") == kind)]
        recent.insert(0, {"kind": kind, "target": target, "args": args})
        s["recent_games"] = recent[:8]
        self.save_settings(s)
