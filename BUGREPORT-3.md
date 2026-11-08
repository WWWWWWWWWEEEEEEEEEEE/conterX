# conterX 第三轮审计

> 环境同上（Python 3.12 / PySide6 6.11）。探针脚本仍在 `Y:/tmp/cxprobe/`。

---

## 一、上一轮的 10 条：全部修复，逐条复验

| 上轮问题 | 复验结果 |
|---|---|
| P1-1 缺运行时 → 整局「引擎异常」 | ✅ 现在 `error=''`，席位记 `crash: 无法启动 p.js：[WinError 2]`，违规 2 → 判负 |
| P1-2 view 里 NaN/set → 整局「引擎异常」 | ✅ 现在 `游戏异常：第 1 回合：发给 p1 的 view 无法序列化为 JSON`（`send` 已把 dumps 包进 try） |
| P1-3 启动握手没有取消通道 | ✅ 玩家 init 睡 10s、1s 时点停止 → **0.6s 退出**（原 9.3s），aborted=True、0 违规 |
| P2-1 收尾串行、每个 0.6s | ✅ 改成 `_close_all` 并发关闭，3 人停止+收尾 0.62s（原 2.3s） |
| P2-2 幽灵 pid 污染排行榜 | ✅ dict 路径已过滤并告警 |
| P2-3 NaN 分数吃掉 report 行 | ✅ 已 `math.isfinite` 校验，report 行回来了 |
| P2-4 splendor `return` 不校验 | ✅ 负数/非整数/超量都抛 `Illegal`，且改成「先算后落盘」，非法动作不再改状态；测试新增了**每种颜色**的守恒断言 |
| P2-5 turn 校验没写文档 | ✅ PROTOCOL.md 第 255 行已写明「必须原样回带」 |
| P3-1 重复 pid 静默去重 | ✅ 会写一条 system 日志 |
| P3-2 `drain()` 连 eof 一起清 | ✅ 只清普通消息，崩溃仍报崩溃 |

三套测试全绿（selftest / splendor 规则 / robustness），GUI 端到端也正常：
splendor 3 人 90 手、sealed_bid 3 人并发 8 回合（47 行日志、详情面板正常）、批量 8 局含 `unstable.py` 无异常无残留进程。

---

## 二、新发现

### P1 · `deadline_ms` 是负数时直接透传，全员 50ms 超时被判负（实测）

`protocol.parse_ask` 只校验「是数字」，不校验正负；`engine._collect_one` 里 `req.deadline_ms or default` 对负数也成立，于是原样进到 `player.act`，`ProcessPlayer.act` 再 `timeout = max(0.05, -500/1000)` → **50ms**。

实测：游戏发 `deadline_ms: -500`，玩家实际只需 200ms

```
p1: 违规 2 | 判负 True
p2: 违规 2 | 判负 True
p3: 违规 2 | 判负 True      ← 三个跑得飞快的玩家，2 回合全灭
```

这个很容易真实发生：游戏按「剩余时间」算 deadline（`remaining = budget - used`），一旦透支就是负数。

顺带一个语义不一致：`deadline_ms: 0` 被 `or` 当成「没指定」，回落到席位超时（实测 5000ms）；负数却是「50ms」。同一个字段三种含义。

**修**：`parse_ask` 里 `if dl is not None and dl <= 0: raise ProtocolError("deadline_ms 必须为正数")`；0 若要保留「用默认值」的语义，就在文档里写清楚。

---

### P2 · 插件游戏返回 `GameResult` 对象时，绕过 `parse_result` 的全部校验（实测）

`core/games.py:314`：

```python
if isinstance(value, GameResult):
    return value                      # ← 直接返回
if isinstance(value, dict):
    return protocol.parse_result(value, pids, on_warn=self._warn)
```

上一轮给 `parse_result` 加的「过滤幽灵 pid」「NaN 分数归零」只作用于 dict 分支。返回 `GameResult`（公开的 dataclass，`_to_result` 明确支持）就全部失效：

```
dict  ->  ranking ['p1']            scores {'p1': 0.0}      ← 校验生效
对象  ->  ranking ['ghost', 'p1']   scores {'p1': nan}      ← 原样放行
```

真跑一局（插件游戏 `return GameResult(...)`）：结算表 `[(1,'ghost',0.0), (2,'p1',nan)]`，
日志最后一行**不是** report（NaN 又让汇总行序列化失败被吞掉了）。

**修**：`GameResult` 分支也走一遍规范化 —— 最简单的做法是 `parse_result(value.__dict__, pids, on_warn=self._warn)`。

---

### P3 · 插件游戏手写 ask 报文出错时，被归成「引擎异常」（实测）

`games.py:295` 的 `return self._to_ask(nxt)` 在 `_advance` 的 `try` 之外，所以 `parse_ask` 抛的 `ProtocolError` 不会变成 `GameFault`：

```
插件 yield {"requests":[{"player":"p1","view":"不是对象"}]}
→ error = 引擎异常：ProtocolError('p1 的 view 必须是对象')
```

（`ctx.ask()` 正常用不会走到这条；只有手写报文才会。）

**修**：`_to_ask` 里 `except ProtocolError as exc: raise GameFault(f"游戏协议错误：{exc}")`。

---

### P3 · 「平均决策耗时」在非常驻模式下把进程启动算进去了（实测）

同一个玩家（只睡 200ms）：

```
常驻模式   : {'p1': 200.6, 'p2': 200.4, 'p3': 200.6}
非常驻模式 : {'p1': 402.0, 'p2': 403.9, 'p3': 403.7}     ← 多出 ~200ms 的进程启动
```

`_ask_one` 的 `t0` 在 `player.act` 之前，而 `act` 里包含 `start()`（spawn + init 握手）。
影响：批量排行榜的「均耗时」列在常驻开关切换后不可比。

**修**：`ProcessPlayer.act` 里把「启动耗时」单独记（`self.start_count` 已经有），`elapsed` 只算发 turn 到收 action 那段；或者统计时把 `start()` 的耗时扣掉。

---

### P3 · 插件游戏 + 模块玩家的 view 现在强制可 JSON 化（行为变化）

`_collect_one` 新增的 `protocol.dumps(req.view)` 校验对**所有**玩家生效。插件游戏和模块玩家本来同进程，以前可以直接传 `datetime`、自定义对象：

```
插件游戏给模块玩家传 datetime
→ 游戏异常：第 1 回合：发给 p1 的 view 无法序列化为 JSON
```

协议本来就规定 view 是 JSON，所以这算「变严」不算「错」，但会打断已有的插件游戏。建议在 `PROTOCOL.md` 第 6 节提一句「即使是模块玩家，view 也必须是可 JSON 化的纯数据」。

---

## 三、还没修的小尾巴（上轮 P3 里剩下的）

| # | 问题 | 说明 |
|---|---|---|
| 1 | 日志/批量目录无清理 | `data/logs` 一局一个文件，长期跑批量会堆到几万个；1MB 的动作也会原样进日志 |
| 2 | 模块玩家超时的线程杀不掉 | 已用 `broken` 标记避免并发，但那个线程会一直空转到进程退出 |
| 3 | `_collect` 上限 16 并发 | 超过 16 个玩家的游戏，后一批会晚开始（每个玩家自己的 deadline 仍然从自己开始算，所以不会误判超时，只是整回合变长） |

---

## 四、结论

三轮下来，核心链路（协议校验、隔离、判罚、并发、取消、收尾）已经相当扎实了。这轮只剩 **1 条 P1（负数 deadline）** 值得马上改 —— 它不会让程序崩，但会让一局跑得完全正常的对局集体判负，而且日志里看着"每个人都超时了"，很容易被误判成玩家的问题。

其余都是边角：`GameResult` 校验旁路、错误归类、统计口径、文档补充。

---

## 修复记录（2026-10-30 晚）

5 条新问题 + 3 条尾巴全部处理，回归用例并入 `tests/test_engine_robustness.py`（现 60 项断言）。

| # | 修法 | 验证 |
|---|---|---|
| P1 负数 deadline | `parse_ask` 拒绝负数（`0` / 省略 = 用默认值，文档写明）；同时拒绝 bool、NaN、Infinity | 游戏发 `-500` → `游戏协议错误：deadline_ms 不能是负数`，**无人判负**；`0` → 走默认 2000ms，0 违规 |
| P2 `GameResult` 绕过校验 | `_to_result` 的 `GameResult` 分支也走 `parse_result(value.__dict__, …, on_warn)` | 插件返回 `GameResult(ranking=["ghost","p1"], scores={"p1": nan})` → 排名 `["p1"]`、分数 0.0 |
| P3 插件 ask 报错归类 | `_to_ask` 里捕获 `ProtocolError` → `GameFault("游戏协议错误：…")` | 手写 `view: "不是对象"` → 报游戏协议错误，不再是「引擎异常」 |
| P3 耗时含进程启动 | 玩家侧记录 `last_setup_ms`（spawn + 握手），引擎算 elapsed 时扣掉 | 同一个 200ms 玩家：常驻 201ms / 非常驻 210ms（原 402ms） |
| P3 插件 view 强制 JSON | 行为保留（协议本来就要求 JSON），在 `PROTOCOL.md` 第 6 节写明「模块玩家也一样」 | — |

### 尾巴

| # | 处理 |
|---|---|
| 1 日志无清理 | `MatchConfig.log_keep`（默认 500）在开局时按 mtime 清理旧日志；单条日志超 64KB 自动截断成摘要 |
| 2 模块玩家超时线程 | 已用 `broken` 标记避免并发，线程本身无法强杀（Python 限制），文档已注明 |
| 3 并发上限 16 | 放宽到 64；超过 16 人的游戏不再分批开始 |

### 顺带

- 席位表的「状态」列不再拿备注填充（原来会显示成「启发式…」），备注改放 tooltip。
