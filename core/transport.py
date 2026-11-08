"""进程传输层：把「子进程 + 一行一条 JSON」封装成可超时、可中断的链路。

特点：
- 后台线程读 stdout，主线程带超时取消息，避免 readline 卡死；
- 支持在等待期间跳过 "log" 之类的旁路消息（交给回调）；
- 进程崩溃 / 输出非 JSON 都能区分出来，便于判定违规。
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import protocol


class LinkError(Exception):
    """链路异常基类。"""


class LinkTimeout(LinkError):
    """等待超时。"""


class LinkClosed(LinkError):
    """对端进程已结束。"""


class BadMessage(LinkError):
    """对端输出了无法解析的内容。"""


class LinkCancelled(LinkError):
    """等待被外部取消（用户点了停止 / 关窗）。"""


def split_args(text: str) -> List[str]:
    """拆命令行参数。Windows 上必须 posix=False，否则 C://models//a.bin 里的反斜杠会被吃掉。"""
    text = (text or "").strip()
    if not text:
        return []
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p
                for p in parts]
    return shlex.split(text)


def python_executable() -> str:
    return sys.executable or "python"


def build_command(target: str, args: Optional[List[str]] = None) -> List[str]:
    """根据文件后缀推断启动命令，让 .py / .exe / .bat / .jar / .js 都能直接导入。"""
    target = os.path.abspath(target)
    ext = os.path.splitext(target)[1].lower()
    if ext in (".py", ".pyw"):
        cmd = [python_executable(), "-u", target]
    elif ext == ".jar":
        cmd = ["java", "-jar", target]
    elif ext in (".js", ".mjs", ".cjs"):
        cmd = ["node", target]
    elif ext == ".sh":
        cmd = ["bash", target]
    else:
        cmd = [target]
    if args:
        cmd.extend(args)
    return cmd


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


class ProcessLink:
    """一条与子进程的 JSON 行链路。"""

    def __init__(self, cmd: List[str], name: str = "process",
                 cwd: Optional[str] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_raw: Optional[Callable[[str, str], None]] = None):
        self.cmd = cmd
        self.name = name
        self.cwd = cwd
        self.on_log = on_log
        self.on_raw = on_raw
        self.proc: Optional[subprocess.Popen] = None
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._stderr_lines: List[str] = []
        self._lock = threading.Lock()

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        kwargs: Dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=_child_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self.cmd, **kwargs)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._q.put(("bad", line))
                    continue
                if not isinstance(msg, dict):
                    self._q.put(("bad", line))
                    continue
                self._q.put(("msg", msg))
        except Exception as exc:  # pragma: no cover - 读管道异常
            self._q.put(("err", str(exc)))
        finally:
            self._q.put(("eof", None))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > 200:
                    del self._stderr_lines[:100]
                if self.on_raw:
                    self.on_raw("stderr", line)
        except Exception:
            pass

    def stderr_tail(self, n: int = 5) -> str:
        return "\n".join(self._stderr_lines[-n:])

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self, graceful: bool = True) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if graceful:
                    try:
                        self.send(protocol.envelope(protocol.M_SHUTDOWN))
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=0.6)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    # -- 收发 --------------------------------------------------------------
    def send(self, obj: Dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise LinkClosed("进程未启动")
        if proc.poll() is not None:
            raise LinkClosed(f"进程已退出（code={proc.returncode}）")
        try:
            data = protocol.dumps(obj) + "\n"
        except (TypeError, ValueError) as exc:
            # 例如 view 里有 NaN / set：这是「对端数据不合法」，不该让整局裸崩
            raise BadMessage(f"消息无法序列化为 JSON：{exc}") from exc
        with self._lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise LinkClosed(f"写入失败：{exc}") from exc

    def drain(self) -> int:
        """丢掉队列里残留的**普通消息**（回合错位后必须清，否则会污染下一回合）。

        但不能连 eof / bad / err 一起清：进程已经崩了的话，下一次 recv 应该报「崩溃」，
        而不是因为标记被清掉、一直等到超时才报「超时」。
        """
        n = 0
        keep = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "msg":
                n += 1
            else:
                keep.append(item)
        for item in keep:
            self._q.put(item)
        return n

    def recv(self, timeout: Optional[float],
             skip: Optional[Callable[[Dict[str, Any]], bool]] = None,
             cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """取一条消息。skip(msg) 返回 True 的消息会被吞掉（例如日志）。

        传了 cancel 就按 50ms 切片轮询，保证「停止」能立刻生效，
        而不是干等到 deadline。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise LinkCancelled("已取消")
            if deadline is None:
                slice_ = 0.05 if cancel is not None else None
            else:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise LinkTimeout("等待超时")
                slice_ = remain if cancel is None else min(remain, 0.05)
            try:
                kind, payload = self._q.get(timeout=slice_)
            except queue.Empty:
                continue
            if kind == "msg":
                if skip and skip(payload):
                    continue
                return payload
            if kind == "bad":
                raise BadMessage(f"输出了非 JSON 内容：{payload[:200]}")
            if kind == "err":
                raise LinkClosed(f"读取异常：{payload}")
            if kind == "eof":
                code = self.proc.poll() if self.proc else None
                tail = self.stderr_tail()
                extra = f"；stderr：{tail}" if tail else ""
                raise LinkClosed(f"进程已结束（code={code}）{extra}")


class InProcessLink:
    """插件式游戏 / 模块式玩家使用的伪链路（同一进程内直接调用）。"""

    def __init__(self, name: str = "inproc"):
        self.name = name
        self.alive = True

    def start(self) -> None:
        self.alive = True

    def close(self, graceful: bool = True) -> None:
        self.alive = False

    def send(self, obj: Dict[str, Any]) -> None:  # pragma: no cover
        pass

    def recv(self, timeout: Optional[float] = None, skip=None) -> Dict[str, Any]:  # pragma: no cover
        raise LinkClosed("插件模式不走链路收发")
