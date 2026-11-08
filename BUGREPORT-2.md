# conterX 第二轮审计（修复验证 + 新问题）

> 环境同上一轮：Python 3.12 / PySide6 6.11。所有「实测」都是跑出来的，脚本在 `Y:/tmp/cxprobe/`。

---

## 一、上一轮的修复验证：全部生效

| 原问题 | 现状 | 实测 |
|---|---|---|
| 同时制被串行化 | `_collect` 改成 ThreadPoolExecutor | 3 人 × 400ms → **401ms**（原 1664ms）；5 人 × 300ms → 302ms |
| NaN 当合法动作 | `dumps(allow_nan=False)` + `_ask_one` 捕获 | 返回 `{'ok': False, 'error': 'protocol', 'violations': 1}` |
| 动作错位 | 校验 `action.turn`，不符则关闭进程 | `[(1,1), (2,'protocol'), (3,3)]`，第 2 回合被抓住 |
| 回合号写死 → 死循环 | `_asks` 计数 + `max_asks` | 6 次后 `游戏异常：疑似死循环`，不再跑满 |
| 游戏协议错误甩锅给引擎 | `_next` 捕获 ProtocolError | `游戏异常：游戏协议错误：result.scores 必须是对象` |
| Windows 参数反斜杠 | `split_args()` posix=False | `['--model', 'C:\\models\\a.bin', '--depth', '6']` |
| 重复 pid 漏关进程 | `_dedupe_pids()` | `['p1', 'p1-2']`，统计两条都在 |
| 失败动作耗时造假 | 真实计时 | 真实 50ms → 上报 50ms |
| 关窗 abort（退出码 127） | `_wait_thread` 等待 + terminate | 关窗退出码 **0**（原 127） |
| 单步模式要点两次 | `_start_match` 同步 `_paused` | 引擎/界面状态一致 |
| players.json 写坏起不来 | `load_*` 全字段兜底 | 不抛异常 |

三套测试全绿：`selftest.py`、`tests/test_splendor_rules.py`（30 局守恒）、`tests/test_engine_robustness.py`。
GUI 端到端也正常：splendor 3 人局 90 手跑完、3 局批量出 CSV、日志 304 行。

---

## 二、新发现

### P1-1 · 运行时缺失会让整局「引擎异常」，一个违规都不记（实测）

`core/players.py:161` 的 `link.start()`（内部是 `subprocess.Popen`）在 `try` **外面**：

```python
link = ProcessLink(...)
link.start()            # ← Popen 在这里，不在 try 里
self.link = link
try:
    link.send(init)
```

所以 `.js` 没装 node / `.jar` 没装 java / `.exe` 没有执行权限时，`FileNotFoundError` 会穿过 `_start_players` 的 `except PlayerFault`，一路冒到 `engine.run` 的兜底 `except Exception`：

```
（PATH 清空 + .js 席位）
error = 引擎异常：FileNotFoundError(2, '系统找不到指定的文件。', None, 2, None)
席位: {'p1': (0, '正常')}          ← 违规 0、状态正常，但整局没了
```

文档承诺「.jar / .js 会按后缀自动选启动方式」，但没装运行时时不是「该玩家启动失败，记违规」，而是整局作废。`core/games.py:136` 的游戏侧同样问题。

**修**：`link.start()` 挪进 try，加 `except OSError as exc: raise PlayerFault("crash", f"无法启动：{exc}", fatal=True)`。

---

### P1-2 · 游戏在 view 里塞了非 JSON 值 → 整局「引擎异常」（实测）

`core/transport.py:203` 的序列化在锁和 try 之外：

```python
data = protocol.dumps(obj) + "\n"     # ← 抛 ValueError/TypeError 没人接
with self._lock:
    try:
        proc.stdin.write(data)
```

上一轮把 `allow_nan=False` 打开之后，游戏只要在 view 里放一个 `float("nan")`（求平均值时分母为 0 就会出）或一个 `set`，就会在 `dumps` 这里炸，裸奔到 `engine.run` 的兜底：

```
view 里放 NaN  → 引擎异常：ValueError('Out of range float values are not JSON compliant: nan')
view 里放 set  → 引擎异常：TypeError('Object of type set is not JSON serializable')
两次的席位统计都是 (0, '正常')      ← 玩家没被记违规，整局作废
```

这算上一轮修复的副作用暴露：以前 NaN 静默通过，现在会在一个没人接的地方抛。

**修**：
1. `send()` 里把 `dumps` 包进 try，转成 `BadMessage("消息无法序列化…")`；
2. `_collect_one` 兜一层 `except Exception` → 记 `protocol` 违规，别让单条坏消息带走整局。

---

### P1-3 · 玩家启动握手没有取消通道，点停止要等满 15s（实测）

`ProcessPlayer.start` 里 `link.recv(start_timeout, skip=...)` 没传 `cancel`（回合内的 `recv` 传了）。

```
玩家 init 后睡 10s 才 ready，1s 时点停止 → 9.3s 后才退出
```

5 个席位都慢启动就是最长 75s，远超 `closeEvent` 的 6s 预算 → 强制 `terminate()` 线程 → 引擎的 `finally` 不执行：日志没有 report 行、玩家收不到 `over`、子进程只能等 EOF 自己退。

**修**：`start()` 的 `recv` 也传 `cancel=self._cancel`（`recv` 已经支持），`start` 的签名加个参数即可。

---

### P2-1 · 停止/关窗的收尾是串行的，每个子进程 0.6s（实测）

`ProcessLink.close()` 先发 `shutdown` 再等 0.6s；`engine.run` 的 finally 是 `for` 循环逐个关。

```
3 个玩家各睡 2s，点停止 → 2326ms 才退出
其中 close: p1 609ms / p2 618ms / p3 442ms / game 617ms
```

取消本身很快（`recv` 50ms 切片），慢的是收尾。5 席位 ≈ 3s，用户会觉得「点了停止没反应」。

**修**：finally 里用线程池并发 close；或对「正在等动作」的玩家直接 terminate 不等 0.6s。

---

### P2-2 · 游戏给的 ranking 里有不存在的 pid，排行榜直接被污染（实测）

`protocol.parse_result` 只去重，不过滤：

```
ranking = ["ghost", "p1"], scores = {"p1": 3}
结算行 = [(1, 'ghost', 0.0), (2, 'p1', 3.0)]     ← ghost 第一，p1 第二
```

批量里 `ranking.index(pid)` 会把所有人的名次往后挤一位，`avg_rank` 全错。游戏里一个拼写错误就能静默毁掉整张榜。

**修**：`parse_result` 里过滤掉不在 `player_ids` 中的 pid（并给个 warning）。

---

### P2-3 · 分数带 NaN 时，日志最后那行 report 静默丢失（实测）

`parse_result` 的 `float(v)` 接受 NaN；`engine.run` 的 finally 里写 report 时 `protocol.dumps` 抛 ValueError，被 `except Exception: pass` 吞掉：

```
scores = {'p1': nan}
日志文件 6 行，最后一行不是 report      ← 汇总行没了
```

report 行恰恰是事后分析最重要的一行。

**修**：`parse_result` 里用 `math.isfinite` 校验，非有限值记 0 并 warning。

---

### P2-4 · 璀璨宝石的 `return` 字段没做范围/类型校验（实测）

`examples/games/splendor.py:452` `_trim`：

```python
take = min(int(v), p.tokens[c], excess)
```

- **负数**：`take` 取到 -100 → 先给玩家「倒加」100 枚，再被自动弃牌循环拉平 → 池子里出现 **6 枚白宝石**（实物只有 5 枚），每种颜色都多 1 枚。
- **非数字**：`int("abc")` 抛 ValueError，但此时 3 枚宝石**已经拿走了**；动作被记「非法」，宝石不回滚 —— 白拿 3 枚。

现有守恒测试只查「池+手总数」，所以两种都测不出来（总数确实没变，变的是每种颜色的分布）。

**修**：`_trim` 里校验 `0 <= v <= 手里该色数量`，越界直接 `Illegal`；`_do_take` 改成「先算出弃哪些、再一起落盘」，别在状态改完之后还可能抛。

---

### P2-5 · 「动作必须回声正确 turn」是新的硬性要求，但文档没写

现在 `action.turn` 和当前回合不一致 → **fatal protocol**（关进程 + 下回合重启）。一个固定 echo `turn: 1` 的老实现会每回合吃一次违规：

```
席位: {'p1': (2, 'protocol: 回合号错位：期望第 3 回合，收到第 1 回合的动作')}
```

行为本身是对的（能抓住错位），但 `PROTOCOL.md` 第 4.2 节和 `使用说明.md` 都没提「turn 会被校验、不符算违规」。自己接程序的人一定踩。

**修**：PROTOCOL.md 的 `action` 一节加一句「`turn` 必须与收到的 `turn` 消息一致，不一致按协议违规处理」；顺便补上「同时制是并发的」「v 接受 "1" / 1.0」。

---

### P3 · 小问题

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | 同一回合重复问同一玩家被静默去重 | `engine.py:363` | 游戏会以为问了两次，只拿回一份；建议记一条 warning |
| 2 | `drain()` 连 eof/bad 标记一起清 | `transport.py:211` | 协议错误后又崩的进程，下一次会被报成「超时」而不是「崩溃」 |
| 3 | 文档没跟上 | `PROTOCOL.md` / `使用说明.md` | 并发询问、`max_asks`、`v` 宽松、turn 校验、停止可能要等几秒，都没写 |

---

## 三、建议顺序

1. **P1-1 / P1-2**：两行 try 的事，但现在都能让整局无声作废，且不留违规记录 —— 最难排查
2. **P1-3**：`start()` 传 cancel，顺手把 P2-1 的并发 close 一起做
3. **P2-2 / P2-3**：`parse_result` 加两个校验
4. **P2-4**：splendor 的 `_trim` 前置校验
5. **P2-5 + P3-3**：补文档

---

## 修复记录（2026-10-30）

11 条全部处理，回归用例已并入 `tests/test_engine_robustness.py`（现 21 组）和 `tests/test_splendor_rules.py`。

| # | 修法 | 验证 |
|---|---|---|
| P1-1 | `ProcessPlayer.start` / `ProcessGame.start` 里把 `link.start()` 挪进 try，`except OSError → PlayerFault("crash")` / `GameFault`；顺带补 `ProtocolError` 归类 | 目录冒充 `.exe` → 只记该席位违规，`report.error` 为空，状态写「无法启动」 |
| P1-2 | `ProcessLink.send` 把 `dumps` 包进 try 转 `BadMessage`；`_collect_one` 在发之前先 `dumps(view)` 预校验，失败抛 `GameFault("…view 无法序列化为 JSON")` | view 里塞 NaN → 「游戏异常：第 1 回合：发给 p1 的 view 无法序列化为 JSON」，不再报引擎异常 |
| P1-3 | `start()` 增加 `cancel` 参数，握手 `recv` 传 `self._cancel`；`LinkCancelled → PlayerAborted` | 玩家 10s 才 ready，1s 时点停止 → **0.62s** 退出 |
| P2-1 | `_close_all()` 用线程池并发关闭所有玩家与游戏 | 3 个进程卡在 sleep(2)，停止+收尾 **0.65s**（串行要 1.9s） |
| P2-2 | `parse_result` 过滤不在 `player_ids` 里的 pid，并通过 `on_warn` 输出告警 | `["ghost","p1","p2"]` → `["p1","p2"]` + 告警 |
| P2-3 | `parse_result` 用 `math.isfinite` 校验分数，非有限值记 0 并告警 | NaN 分数 → 0.0，日志最后一行仍是 report |
| P2-4 | 璀璨宝石 `_do_take` / `_do_reserve` 改为「先在副本上算、全部校验通过再落盘」；`_trim` 拆成 `_plan_trim`（只算，非法即抛）+ `_apply_trim` | 负数/非整数/超额/弃多了/未知颜色全部被拒，且手牌与池子**一个字节都没变**；守恒测试升级为**逐色**核对 |
| P2-5 | `PROTOCOL.md` 4.2 已补「turn 必须原样回带」；`使用说明.md` 加 FAQ 说明会被校验 | — |
| P3-1 | 去重时输出一条系统日志 | — |
| P3-2 | `drain()` 只清普通消息，保留 `eof/bad/err` 终止标记 | 进程秒退 → drain 后 `recv` 报 `closed` 而不是 `timeout` |
| P3-3 | `使用说明.md` 补 4 条 FAQ：turn 校验、并发询问、停止耗时、缺运行时 | — |

### 顺带说明

- `parse_result` 新增可选参数 `on_warn`，`ProcessGame` / `PluginGame` 都接上了，告警会以 `⚠` 前缀进回合日志。
- 璀璨宝石的守恒测试从「总数」升级为「逐色 + 黄金」，正是这个改动让 P2-4 那类污染再也藏不住。
