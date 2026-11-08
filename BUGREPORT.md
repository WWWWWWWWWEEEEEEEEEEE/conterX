# conterX 潜在问题清单

> 审计方式：读完全部源码（core / ui / sdk / examples / tests），并在 Python 3.12 + PySide6 6.11 上实跑验证。
> `selftest.py` 四个场景、`tests/test_splendor_rules.py` 全部通过 —— 下面这些是**测试没覆盖到**的路径。
> 标「已复现」的，都跑过最小用例，结论来自实测输出，不是猜的。

---

## P0 · 会毁掉一局结果 / 让程序崩掉

### 1. 对局进行中点「关闭窗口」→ 进程异常退出（已复现）

`ui/main_window.py:911` `closeEvent` 只等 3 秒：

```python
self.thread.engine.stop()
self.thread.wait(3000)      # 超时就继续往下走
```

`stop()` 只在下一次 `_gate()` 生效（`engine.py:149`），**打断不了正在进行的回合**。回合里每个玩家还在等自己的 deadline，3 个席位 × 2000ms 就超过 3 秒，`wait` 超时返回，窗口关闭、解释器退出时 QThread 还在跑 → Qt 直接 abort。

实测（3 个玩家各睡 5s、超时 60s，中途关窗）：

```
close() 返回耗时 3.0s；线程还在跑吗: True
EXITCODE=127      # 与「QThread destroyed while running」的最小复现退出码完全一致
```

对照实验：单独一个 `QThread` 里 `sleep(10)`，主程序直接退出 → 同样是 127。

**建议**：`closeEvent` 里先 `stop()`，再循环 `wait(100)` 最多等 ~5s，仍未退出就 `terminate()`；或者把 `stop()` 做成能中断在途回合（见第 7 条）。至少不要在没有确认线程结束的情况下放行 `closeEvent`。

---

### 2. 「同时制」实际是串行问的（已复现）

`PROTOCOL.md` 明确写：

> `requests` 里放几个人，就是**同时**问几个人

`engine.py:319` `_collect` 却是一个 for 循环逐个 `_ask_one`：

```python
for req in ask.requests:
    ...
    ar = self._ask_one(player, st, ask.turn, req.view, deadline)
```

实测：3 个玩家同时被问、每人睡 400ms、deadline 800ms

```
引擎报告用时: 1830 ms   ← 应是 ~400ms
```

后果不只是慢：一个回合的墙钟时间 = Σ 每个人的 deadline。默认 2000ms + 5 个席位 = 单回合最坏 10s；璀璨宝石 deadline 3000ms、5 人局 100 手 → 一局 25 分钟。而且 p1 卡住会直接吃掉 p2 的思考时间，违反同时出价的语义（暗标竞拍这类游戏会被影响）。

**建议**：`ThreadPoolExecutor(max_workers=len(requests))` 并发跑 `_ask_one`；顺带把 `_emit` / `st.violations` 的写入加锁。

---

### 3. 玩家多吐一条 `action` → 整局动作错位（已复现）

`players.py:177` `ProcessPlayer.act` 只从队列里取**一条**消息，`protocol.py:217` `parse_action` 也**完全不看 `msg["turn"]`**。

于是只要玩家在某一回合输出了两行 `action`（算法打印两次、缓冲区重复写、模板写错），多出来的那行会永远留在队列里，被下一回合当成答案：

```
第 1 回合：引擎取到 n=1，多出的 n=1(stale) 留在队列
第 2 回合：引擎取到 n=1(stale)      ← 错位
第 3 回合：引擎取到 n=2
游戏收到的动作: [(1, 1), (2, 1), (3, 2)]
引擎错误: 无                        ← 全程无告警
```

这是最阴的一类 bug：不报错、不记违规、日志里看起来正常，但整局数据是错的。

同类问题：**协议违规（返回 null / 类型不对）之后链路也不重置**，`players.py:204` 只抛 `PlayerFault("protocol")`，进程和队列原样留着，下一回合照样可能读到残留消息。

**建议**：
- `parse_action` 校验 `msg.get("turn")` 与当前回合一致，不一致按 `protocol` 违规处理；
- `protocol` 类违规后强制 `self.close()` 重启进程（或至少 drain 掉队列里多余的消息）。

---

### 4. `NaN` / `Infinity` 会被当成合法动作发给游戏（已复现）

`protocol.py:109` `dumps` 没关 `allow_nan`，而 `engine.py:358` 的可序列化检查用的也是同一个函数：

```
ActionResult: {'ok': True, 'action': {'bid': nan}}
发给游戏的报文: {"v":1,"type":"actions","turn":1,"actions":{"p1":{...,"action":{"bid":NaN}}}}
→ 严格 JSON 解析失败： NaN
```

Python 的 `json.loads` 认 `NaN`，所以本地自测全绿；但 Node / Rust / C++ / Java 的解析器一律报错 —— 恰好违反「跨语言、NDJSON 严格 JSON」这个立身之本。分数里出现 NaN 还会污染批量 CSV。

**建议**：`dumps` 加 `allow_nan=False`，在 `_ask_one` 里捕获 `ValueError` 记 `protocol` 违规（这样玩家侧的问题就变成可判罚的违规，而不是把毒数据传给游戏）。

---

## P1 · 特定输入下会卡死 / 判罚错

### 5. 回合号不递增时没有兜底，`turns` 统计也是错的（已复现）

`engine.py:229` 只校验 `state.turn > MAX_TURNS`，但 `turns = max(turns, state.turn)`。一个把 `turn` 写死成 1 的游戏（很常见的手写错误）永远触发不了上限：

```
游戏连发 60 次 turn=1 → 结果: turns=1 用时=24.39s error=''
```

真跑起来就是无限循环，只能靠用户手动点「停止」；报告里的「回合数」还显示 1。

**建议**：另加一个 `_asks` 计数器（每收一次 `ask` +1）与 `MAX_TURNS` 比较，`turns` 也用 `max(turns, self._asks)`。

### 6. 停止 / 关窗打断不了在途回合（已复现）

`engine.py:149` `_gate` 是唯一检查 `_stopped` 的地方。3 个玩家各睡 400ms，第 1.2s 点停止：

```
点停止后还要等 0.71s 才真正退出
```

机制上就是「当前回合剩下的玩家照问完」。deadline 设 60s 时，点停止要等满一分钟。这是第 1 条的直接成因。

**建议**：`_collect` 里每次 `_ask_one` 前检查 `_stopped`；`ProcessPlayer.act` 的 `recv` 改成可被 `stop` 唤醒（比如等待时轮询一个 Event，超时粒度 50ms）。

### 7. 游戏侧协议错误被归类成「引擎异常」（已复现）

`games.py:175` `_next` 只捕获 `LinkTimeout / LinkClosed / BadMessage`，`check_envelope` / `parse_ask` / `parse_result` 抛的 `ProtocolError` 会一路冒到 `engine.py:251` 的兜底 `except Exception`：

```
游戏发了不合法的 over（scores 是数组）
report.error = 引擎异常：ProtocolError('result.scores 必须是对象')
```

界面提示会把锅甩给中转站自己，排查时容易走错方向。

**建议**：`_next` 里 `except ProtocolError as e: raise GameFault(f"游戏协议错误：{e}")`。

### 8. Windows 下「启动参数」里的反斜杠会被吃掉（已复现）

`players.py:62` 用 `shlex.split`（POSIX 模式）切参数，反斜杠被当转义符：

```
args = --model C:\models\a.bin --depth 6
shlex.split → ['--model', 'C:modelsa.bin', '--depth', '6']       ← 路径被毁
```

`games.py:129` 又换成 `self.args.split()`，两边行为不一致（`.split()` 反而不会毁路径，但没法处理带空格的参数）。

**建议**：Windows 上 `shlex.split(args, posix=False)`，或干脆存成 JSON 数组；两处统一。

### 9. 席位 pid 重复时会漏关子进程（已复现）

`engine.py:288` `_start_players` 直接 `self.players[seat.pid] = player`，重复 pid 时前一个玩家对象被覆盖，它的子进程再也没人关：

```
跑之前的 python 进程数: 1
跑完 1.5s 后的 python 进程数: 2      ← 残留一个
seats 统计条目数: 1                  ← 两个席位只统计一个
```

UI 的 `_renumber()` 能避免，但 `data/players.json` 是手改得动的。

**建议**：`MatchEngine.__init__` 里对 pid 去重（或直接报错），`_start_players` 发现重复时先关旧的。

---

## P2 · 健壮性 / 体验

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 10 | `v` 字段校验过严 | `protocol.py:130` | `"1"` / `1.0` 被拒，报错还是「协议版本不支持：1（本机支持 1）」这种自相矛盾的话；而 `true` 反而能过（bool 是 int 子类）。跨语言实现很容易踩。 |
| 11 | 配置文件损坏 → 启动即崩 | `store.py:70`、`main_window.py:203` | `players.json` 写成 `[1,2,3]` → `AttributeError: 'int' object has no attribute 'setdefault'`；`settings.json` 里 `seed: "abc"` → `int()` 抛 ValueError。两处都在 `MainWindow.__init__` 路径上，直接起不来。建议 `load_*` 全字段 try/except + 类型兜底。 |
| 12 | 失败动作的耗时是假的 | `engine.py:385` | 真实 50ms 崩溃，上报 `elapsed_ms: 5000`（= deadline）。游戏侧据此做的超时分析全是错的。 |
| 13 | `ranking` 不去重 | `protocol.py:189` | 游戏返回 `["p1","p1","p2"]` → 结算表出现两行「玩家1」，名次 1、2。 |
| 14 | 单步模式的暂停状态不同步 | `main_window.py:620/657` | `_start_match(True)` 暂停了引擎，却没把 `self._paused` 置 True。实测：引擎 `paused=True`、界面 `_paused=False`、按钮显示「暂停」，用户得点两次才能真正继续。 |
| 15 | 插件游戏 `requests: []` 与进程式不一致 | `games.py:270` | 插件 yield 空 requests → 游戏收到 `actions = {}`，无报错；进程式游戏发空 requests 会被 `parse_ask` 判协议错误。同一个协议两种行为。 |
| 16 | 日志 `_seq` 存在竞争 | `engine.py:162` | 玩家 stderr 是独立线程回调 `_emit`，与引擎线程并发 `self._seq += 1` 和写文件。目前靠 GIL 侥幸不出事，但 seq 可能重复、日志行可能交错。 |
| 17 | 模块式玩家超时后线程仍在跑 | `players.py:262` | 文档已说明「杀不掉」，但没说明**下一回合会开新线程并发跑同一个 `decide`**，模块全局变量会被两个线程同时改。建议超时后把这个玩家标记为「不可信」，本局不再询问。 |
| 18 | 批量 CSV 可能互相覆盖 | `batch.py:161` | 文件名只到秒，同一秒内跑两次批量就覆盖了。 |
| 19 | `recent_games` 是死代码 | `store.py:96` | `remember_game()` 全项目无人调用，「最近使用的游戏」永远不会出现。 |

---

## 已经确认没问题的地方

- 私有视野隔离正确：p1 只收到自己那份 `view`，没有交叉投递。
- 违规判定主链路正确：超时 / 崩溃 / 非 JSON / null 动作都能记违规，`max_violations=0` 一次即判负也对；`retry_invalid=True` 实测确实重试 2 次、只记 1 次违规。
- 常驻/非常驻两种玩家、进程/插件两种游戏、批量对抗的统计口径都对。
- 璀璨宝石规则自测 30 局守恒（宝石总量 / 90 张卡 / 分数）全过，非法动作不会破坏状态。
- `selftest.py` 四个场景全绿；界面 offscreen 起得来，能跑完整一局（4 回合、31 条日志、3 行结算）。

---

## 建议修复顺序

1. **#3 消息错位 + `turn` 校验**（静默污染数据，最难事后发现）
2. **#1 关窗崩溃** + **#6 停止打断**（用户每天都会遇到）
3. **#2 并发询问**（协议承诺 vs 实现不符，直接影响一局时长）
4. **#4 NaN**（一行 `allow_nan=False` 就能修）
5. 其余按 P1 → P2 顺手清

---

## 修复记录（2026-10-29）

19 条全部处理。回归用例在 `tests/test_engine_robustness.py`（12 组），UI 部分单独手测。

| # | 修法 | 验证 |
|---|---|---|
| 1 | `closeEvent` 改为 `_wait_thread()`：发停止 → 等 6s → 仍未停则 `terminate()` + 再等 1.5s；仍失败则 `event.ignore()` 并提示，绝不放行 | 对局中关窗：退出码 **0**（原 127），耗时 1.94s |
| 2 | `_collect` 改用 `ThreadPoolExecutor`，多请求并发下发；日志写入加锁 | 3 人各睡 400ms，单回合 **401ms**（原 1830ms） |
| 3 | `ProcessPlayer.act` 校验 `msg["turn"]`，不一致按 `protocol` 违规并重启进程；协议错误后 `link.drain()` 清残留 | 玩家多吐一条 action：不再错位，记违规 |
| 4 | `protocol.dumps` 加 `allow_nan=False`，`_ask_one` 捕获 `ValueError` 记违规 | NaN 动作 → `ok:false`，游戏收不到 NaN |
| 5 | 新增 `_asks` 计数（不受游戏自报 turn 影响），上限可配 `MatchConfig.max_asks` | turn 写死成 1 的游戏 30 次内被中断 |
| 6 | 引擎持有 `_cancel` 事件，`recv` 按 50ms 切片轮询；`_collect_one` 入口也检查 | 3 人各睡 10s，点停止 **0.12s** 退出（原要等满 deadline） |
| 7 | `ProcessGame._next/start` 捕获 `ProtocolError` → `GameFault("游戏协议错误：…")` | 非法 `over` 现在报「游戏异常：游戏协议错误」 |
| 8 | 新增 `transport.split_args`：Windows 用 `posix=False` 并剥引号，玩家与游戏统一 | `C:/models/a.bin` 不再被破坏，带空格参数正常 |
| 9 | `MatchEngine._dedupe_pids()` 改名去重并在日志里告警 | 重复 pid → 两个席位都保留，各关各的进程 |
| 10 | `check_envelope` 兼容 `"1"`/`1.0`，拒绝 `true` 与高版本，错误信息不再自相矛盾 | 6 种取值逐条验证 |
| 11 | `load_players` 逐条 try/except + 类型兜底；`load_settings` 按默认值类型强校验；`SeatConfig.from_dict` 安全转型 | 坏 JSON / `seed:"abc"` / `game:"oops"` 均可启动 |
| 12 | 失败动作上报 `perf_counter` 实测耗时，不再用 deadline 充数 | 50ms 崩溃 → `elapsed_ms≈50`（原 5000） |
| 13 | `parse_result` 对 `ranking` 去重 | `["p1","p1","p2"]` → `["p1","p2"]` |
| 14 | 单步启动后同步 `self._paused=True` 与按钮文案 | 单步后 `paused=True`、按钮显示「继续」 |
| 15 | `PluginGame._to_ask` 拒绝空 `requests`，与进程式行为一致 | 插件 `ctx.ask([])` → 报错而不是静默跑完 |
| 16 | `_emit` 全程持 `_log_lock`，`seq` 与文件写入串行 | 并发询问下日志序号连续 |
| 17 | `ModulePlayer.broken`：超时后本局不再调用 `decide` | 文档同步更新 |
| 18 | 批量 CSV 文件名带毫秒 + 存在性检查，同毫秒也不覆盖 | 连续两次批量产出两个文件 |
| 19 | `_edit_game` 调用 `remember_game`，游戏对话框加「最近使用」下拉 | 下拉可回填路径/类型 |

### 顺带加固（报告外）

- **同回合重复请求同一玩家会被去重**：否则并发下两个线程会同时操作同一个子进程。
- **批量停止能打断当前这一局**：`BatchRunner.stop()` 现在会顺手 `engine.stop()`，不用等这一局跑完。
- **`MatchConfig.max_asks`**：询问次数上限变成可配置项，方便压测与自定义长局。
